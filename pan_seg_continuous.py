import os
import time
import argparse

import torch
import numpy as np
from scipy.spatial import KDTree

from WaffleIron.waffleiron import Segmenter
import WaffleIron.utils.transforms as tr

from utils.clustering import Clusterer
from utils.association import association, long_association
from utils.misc import (
    Obj_cache,
    save_data,
    process_configs,
    print_config_cont,
    load_config,
    transform_pointcloud,
)


class PanSegmenter:
    def __init__(self, args):
        self.args = args

        # Set device
        device = "cpu"
        if torch.cuda.is_available():
            if args.gpu is not None:
                device = f"cuda:{args.gpu}"
            else:
                device = "cuda"
        elif torch.mps.is_available():
            device = "mps"
            torch.set_default_dtype(torch.float32)
        self.device = torch.device(device)
        args.gpu = device

        # Load config files
        config_panseg = load_config("configs/config.yaml")
        config_pretrain = load_config(args.config_pretrain)
        config_model = load_config(config_panseg[args.dataset]["config_downstream"])

        process_configs(args, config_panseg, config_pretrain, config_model)
        self.config_msg = print_config_cont(args, config_panseg)
        if args.save_path is not None:
            if not os.path.exists(args.save_path):
                os.makedirs(args.save_path)
            with open(f"{args.save_path}/config.txt", "w") as f:
                f.write(self.config_msg)

        self.config = config_panseg

        # Init preprocessing
        self.input_feat = config_model["embedding"]["input_feat"]
        self.mean_int = args.mean_int
        self.std_int = args.std_int

        self._downsample = tr.Voxelize(
            dims=(0, 1, 2),
            voxel_size=config_model["embedding"]["voxel_size"],
            random=False,
        )

        fov_xyz = config_model["waffleiron"]["fov_xyz"]
        assert len(fov_xyz[0]) == len(fov_xyz[1]), (
            "Min and Max FOV must have the same length."
        )
        for i, (min, max) in enumerate(zip(*fov_xyz)):
            assert min < max, (
                f"Field of view: min ({min}) < max ({max}) is expected on dimension {i}."
            )
        self.fov_xyz = np.concatenate([np.array(f)[None] for f in fov_xyz], axis=0)
        self._crop_to_fov = tr.Crop(dims=(0, 1, 2), fov=fov_xyz)

        grids_shape = config_model["waffleiron"]["grids_size"]
        dim_proj = config_model["waffleiron"]["dim_proj"]
        assert len(grids_shape) == len(dim_proj)
        self.dim_proj = dim_proj
        self.grids_shape = [np.array(g) for g in grids_shape]
        self.lut_axis_plane = {0: (1, 2), 1: (0, 2), 2: (0, 1)}

        num_neighbors = config_model["embedding"]["neighbors"]
        assert num_neighbors > 0
        self.num_neighbors = num_neighbors

        # Build network
        self.model = Segmenter(
            input_channels=config_model["embedding"]["size_input"],
            feat_channels=config_model["waffleiron"]["nb_channels"],
            depth=config_model["waffleiron"]["depth"],
            grid_shape=config_model["waffleiron"]["grids_size"],
            nb_class=config_model["classif"]["nb_class"],
            drop_path_prob=config_model["waffleiron"]["drop_path"],
            layer_norm=config_model["waffleiron"]["layernorm"],
        )

        # Adding classification layer
        classif = torch.nn.Conv1d(
            config_model["waffleiron"]["nb_channels"],
            config_model["classif"]["nb_class"],
            1,
        )
        torch.nn.init.constant_(classif.bias, 0)
        torch.nn.init.constant_(classif.weight, 0)
        self.model.classif = torch.nn.Sequential(
            torch.nn.BatchNorm1d(config_model["waffleiron"]["nb_channels"]),
            classif,
        )

        # Load pretrained model
        ckpt = torch.load(args.pretrained_ckpt, map_location="cpu", weights_only=True)
        ckpt = ckpt["net"]
        new_ckpt = {}
        for k in ckpt.keys():
            if k.startswith("module"):
                new_ckpt[k[len("module.") :]] = ckpt[k]
            else:
                new_ckpt[k] = ckpt[k]

        # Set model to evaluation mode
        self.model.load_state_dict(new_ckpt)
        self.model = self.model.to(device)
        if torch.cuda.is_available():
            self.model.compile()
        self.model.eval()

        # Initialize
        self.prev_ind = None
        self.prev_scene = None
        self.prev_points = None
        self.clusterer = Clusterer(config_panseg)
        self.obj_cache = Obj_cache(config_model["classif"]["nb_class"])

    def _get_occupied_2d_cells(self, pc):
        """Return mapping between 3D point and corresponding 2D cell"""
        cell_ind = []
        for dim, grid in zip(self.dim_proj, self.grids_shape):
            # Get plane of which to project
            dims = self.lut_axis_plane[dim]
            # Compute grid resolution
            res = (self.fov_xyz[1, dims] - self.fov_xyz[0, dims]) / grid[None]
            # Shift and quantize point cloud
            pc_quant = ((pc[:, dims] - self.fov_xyz[0, dims]) / res).astype("int")
            # Check that the point cloud fits on the grid
            min, max = pc_quant.min(0), pc_quant.max(0)
            assert min[0] >= 0 and min[1] >= 0, print(
                "Some points are outside the FOV:", pc[:, :3].min(0), self.fov_xyz
            )
            assert max[0] < grid[0] and max[1] < grid[1], print(
                "Some points are outside the FOV:", pc[:, :3].min(0), self.fov_xyz
            )
            # Transform quantized coordinates to cell indices for projection on 2D plane
            temp = pc_quant[:, 0] * grid[1] + pc_quant[:, 1]
            cell_ind.append(temp[None])
        return np.vstack(cell_ind)

    def _prepare_input_features(self, pc_orig):
        # Concatenate desired input features to coordinates
        pc = [pc_orig[:, :3]]  # Initialize with coordinates
        for type in self.input_feat:
            if type == "intensity":
                intensity = pc_orig[:, 3:]
                intensity = (intensity - self.mean_int) / self.std_int
                pc.append(intensity)
            elif type == "height":
                pc.append(pc_orig[:, 2:3])
            elif type == "radius":
                r_xyz = np.linalg.norm(pc_orig[:, :3], axis=1, keepdims=True)
                pc.append(r_xyz)
            elif type == "xyz":
                xyz = pc_orig[:, :3]
                pc.append(xyz)
            elif type == "constant":
                pc.append(np.ones((pc_orig.shape[0], 1)))
            else:
                raise ValueError(f"Unknown feature: {type}")
        return np.concatenate(pc, 1)

    def _preprocess(self, data):
        # Prepare input feature
        pc_orig = self._prepare_input_features(data["points"])

        # Voxelization
        pc, _ = self._downsample(pc_orig, None)

        # Crop to fov
        pc, _ = self._crop_to_fov(pc, None)
        feat = pc[:, 3:].T

        # For each point, get index of corresponding 2D cells on projected grid
        cell_ind = self._get_occupied_2d_cells(pc)

        # Get neighbors for point embedding layer providing tokens to waffleiron backbone
        kdtree = KDTree(pc[:, :3])
        assert pc.shape[0] > self.num_neighbors
        _, neighbors_emb = kdtree.query(pc[:, :3], k=self.num_neighbors + 1)

        # Nearest neighbor interpolation to undo cropping & voxelisation
        _, upsample = kdtree.query(pc_orig[:, :3], k=1)

        out = {
            "feat": torch.from_numpy(feat[None]).to(self.device),
            "neighbors_emb": torch.from_numpy(neighbors_emb.T[None]).to(self.device),
            "cell_ind": torch.from_numpy(cell_ind[None]).to(self.device),
            "occupied_cells": torch.ones(feat.shape[-1]).unsqueeze(0).to(self.device),
            "upsample": torch.from_numpy(upsample).to(self.device),
            "ego": torch.from_numpy(data["ego"]).to(self.device),
            "scene": data["scene"],
            "sample": data["sample"],
        }

        return out

    def __call__(self, data):
        times = [time.time()]

        # network inputs
        data = self._preprocess(data)
        net_inputs = (
            data["feat"],
            data["cell_ind"],
            data["occupied_cells"],
            data["neighbors_emb"],
        )
        times.append(time.time())

        # get semantic class prediction
        with torch.inference_mode():
            out, tokens = self.model(*net_inputs)
        out = out[0].argmax(dim=0)
        times.append(time.time())

        # upsample to original resolution
        out_upsample = out[data["upsample"]]

        # get instance prediction
        src_points = data["feat"][0, 1:4, data["upsample"]].T
        src_features = tokens[0, :, data["upsample"]].T

        # ego motion compensation
        src_points_ego = transform_pointcloud(src_points, data["ego"])

        # get semantic class
        src_pred = out_upsample.unsqueeze(1)

        # clustering
        src_points = torch.cat((src_points, src_pred), axis=1)
        src_labels = self.clusterer.get_semantic_clustering(src_points)

        # create data - ego compensated xyz + features + semantic class + cluster id
        src_points = torch.cat(
            (src_points_ego, src_features, src_pred, src_labels.unsqueeze(1)), axis=1
        )
        times.append(time.time())

        # associate -- set temporally consistent instance id
        ind_src = None
        if (
            self.prev_scene is None
            or not self.prev_scene["token"] == data["scene"]["token"]
        ):
            self.prev_ind = None
            self.obj_cache.reset()
            self.prev_points = torch.zeros_like(src_points)

        if self.config["association"]["use_long"]:
            _, ind_src = long_association(
                self.prev_points,
                src_points,
                self.config,
                self.prev_ind,
                self.obj_cache,
                None,
            )
        else:
            _, ind_src = association(
                self.prev_points,
                src_points,
                self.config,
                self.prev_ind,
                self.obj_cache,
                None,
            )
        self.prev_ind = ind_src
        self.obj_cache.max_id = int(
            max(self.obj_cache.max_id, self.prev_ind.max(), ind_src.max())
        )

        self.prev_points = src_points
        self.prev_scene = data["scene"]
        times.append(time.time())

        if self.args.verbose:
            print(
                f"Total time: {times[-1] - times[0]:.2f} s\n"
                f"  SemSeg data prep: {times[1] - times[0]:.2f} | "
                f"Semantic segmentation: {times[2] - times[1]:.2f} | "
                f"InsSeg data prep: {times[3] - times[2]:.2f} | "
                f"Instance association: {times[4] - times[3]:.2f} | "
            )

        src_pred = src_pred.cpu().numpy().squeeze()
        ind_src = ind_src.cpu().numpy()

        # save segmentation files
        if self.args.save_path is not None:
            if self.args.dataset == "semantic_kitti":
                src_pred = self.mapper(src_pred)
            save_data(
                self.args.save_path,
                data["scene"]["name"],
                data["sample"],
                src_pred,
                ind_src,
            )

        return src_pred, ind_src

    def __str__(self):
        return f"PanSegmenter({self.config_msg})"


def parse_args():
    parser = argparse.ArgumentParser(description="4D Panoptic Segmentation")
    parser.add_argument(
        "--dataset",
        type=str,
        help="Dataset name",
        default="pone",
    )
    parser.add_argument(
        "--path_dataset",
        type=str,
        help="Path to dataset",
        default="/mnt/personal/vlkjan6/PONE/val",
    )
    parser.add_argument(
        "--config_pretrain",
        type=str,
        required=False,
        default="ScaLR/configs/pretrain/WI_768_pretrain.yaml",
        help="Path to config for pretraining",
    )
    parser.add_argument(
        "--config_downstream",
        type=str,
        required=False,
        default="ScaLR/configs/downstream/semantic_kitti/WI_768_linprob.yaml",
        help="Path to model config downstream",
    )
    parser.add_argument(
        "--pretrained_ckpt",
        type=str,
        default="ScaLR/logs/linear_probing/WI_768-DINOv2_ViT_L_14-NS_KI_PD/semantic_kitti/ckpt_last.pth",
        help="Path to pretrained ckpt",
    )
    parser.add_argument(
        "--gpu", default=None, type=int, help="Set to a number of gpu to use"
    )
    parser.add_argument(
        "--save_path", type=str, default=None, help="Path to save segmentation files"
    )
    parser.add_argument(
        "--clustering", type=str, default=None, help="Clustering method"
    )
    parser.add_argument(
        "--short",
        action="store_true",
        default=False,
        help="Do not use long association",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=False, help="Verbose mode"
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    args.workers = 0

    # Set parameters -- necessary for the dataloader
    # not needed in real deployment
    args.eval = True
    args.test = False
    args.flow = False
    args.batch_size = 1

    args.dataset = args.dataset.lower()
    if args.dataset not in ["pone", "semantic_kitti"]:
        raise ValueError(f"Dataset {args.dataset} not available.")

    if args.dataset == "pone":
        args.mean_int = 0.391358
        args.std_int = 0.151813
    elif args.dataset == "semantic_kitti":
        args.mean_int = 0.28613698
        args.std_int = 0.14090556

    # Initialize segmenter
    segmenter = PanSegmenter(args)

    try:
        for i, item in enumerate(sorted(os.listdir(args.path_dataset))):
            if args.dataset == "pone":  # load PONE dataset
                file = np.load(os.path.join(args.path_dataset, item), allow_pickle=True)
                scene_name = item.split("/")[-1][:-9]
                scene = {"name": scene_name, "token": scene_name}
                data = {
                    "points": file["pcd"],
                    "ego": file["odom"]["transformation"],
                    "scene": scene,
                    "sample": item,
                }
            elif args.dataset == "semantic_kitti":  # load SemanticKITTI dataset
                pcd = np.fromfile(
                    os.path.join(args.path_dataset, item), dtype=np.float32
                ).reshape(-1, 4)
                poses = np.loadtxt(
                    os.path.join(args.path_dataset, "../poses.txt")
                ).reshape(-1, 3, 4)
                pose_t = np.vstack([poses[i], np.array([0, 0, 0, 1])])
                pose_0 = np.vstack([poses[0], np.array([0, 0, 0, 1])])
                scene_name = args.path_dataset.split("/")[-2]
                scene = {"name": scene_name, "token": scene_name}
                data = {
                    "points": pcd,
                    "ego": (np.linalg.inv(pose_0) @ pose_t).astype(np.float32),
                    "scene": scene,
                    "sample": item,
                }
            else:
                raise ValueError(f"Dataset {args.dataset} not available.")

            # Call segmenter
            _, _ = segmenter(data)
    except KeyboardInterrupt:
        print("Keyboard interrupt, exiting...")
    except Exception as e:
        raise e
