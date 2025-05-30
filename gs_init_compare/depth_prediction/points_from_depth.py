import logging
from pathlib import Path
from typing import Optional
import numpy as np
import torch

from gs_init_compare.datasets.colmap import Parser
from gs_init_compare.depth_alignment import (
    DepthAlignmentStrategyEnum,
    DepthAlignmentStrategy,
    DepthAlignmentParams,
)
from gs_init_compare.depth_prediction.predictors.depth_predictor_interface import (
    PredictedDepth,
)
from gs_init_compare.depth_subsampling.interface import DepthSubsampler
from gs_init_compare.nerfbaselines_integration.method import (
    gs_Parser as NerfbaselinesParser,
)
from gs_init_compare.depth_prediction.utils.point_cloud_export import (
    export_point_cloud_to_ply,
)

_LOGGER = logging.getLogger(__name__)


class LowDepthAlignmentConfidenceError(Exception):
    pass


def debug_export_point_clouds(
    imsize,
    cam2world,
    P,
    transform_c2w,
    sfm_points,
    pts_world,
    masked_out_world,
    parser,
    image_name,
    downsample_mask,
    rgb_image,
    dir=Path("ply_export_debug"),
):
    camera_plane = torch.dstack(
        [
            torch.from_numpy(np.mgrid[0 : imsize[0], 0 : imsize[1]].T).to(
                cam2world.device
            ),
            torch.ones(imsize, device=cam2world.device).T,
        ]
    ).reshape(-1, 3)

    dir = Path(dir)
    dir.mkdir(exist_ok=True, parents=True)

    export_point_cloud_to_ply(
        transform_c2w(camera_plane).reshape(-1, 3).cpu().numpy(),
        rgb_image.reshape(-1, 3).cpu().numpy() if rgb_image is not None else None,
        dir,
        "camera_plane",
    )
    sfm_points_repro_world = P @ torch.vstack(
        [sfm_points.T, torch.ones(sfm_points.shape[0], device=cam2world.device)]
    )
    sfm_points_repro_world = sfm_points_repro_world / sfm_points_repro_world[2]
    sfm_pt_rgbs = parser.points_rgb[parser.point_indices[image_name]] / 255.0

    export_point_cloud_to_ply(
        transform_c2w(sfm_points_repro_world.T).reshape(-1, 3).cpu().numpy(),
        sfm_pt_rgbs,
        dir,
        "sfm_points_plane_proj",
    )
    export_point_cloud_to_ply(
        sfm_points.reshape(-1, 3).cpu().numpy(),
        sfm_pt_rgbs,
        dir,
        "sfm_points_world",
    )
    rgbs = None
    if rgb_image is not None:
        rgbs = rgb_image.view(-1, 3)[downsample_mask]
        rgbs = rgbs.cpu().numpy()

    export_point_cloud_to_ply(
        pts_world.reshape(-1, 3).cpu().numpy(),
        rgbs,
        dir,
        "my_points_world",
    )

    if masked_out_world.shape[0] == 0:
        return

    red = np.array([1, 0, 0])
    export_point_cloud_to_ply(
        masked_out_world.cpu().numpy(),
        np.tile(red, (masked_out_world.shape[0], 1)),
        dir,
        "my_outliers_world",
    )


def get_valid_sfm_pts(sfm_pts_camera, sfm_pts_camera_depth, mask, imsize):
    valid_sfm_pt_indices = torch.logical_and(
        torch.logical_and(sfm_pts_camera[0] >= 0, sfm_pts_camera[0] < imsize[0]),
        torch.logical_and(sfm_pts_camera[1] >= 0, sfm_pts_camera[1] < imsize[1]),
    )
    valid_sfm_pt_indices = torch.logical_and(
        valid_sfm_pt_indices, sfm_pts_camera_depth >= 0
    )
    print(
        f"Num invalid reprojected SfM points: {sfm_pts_camera.shape[1] - torch.sum(valid_sfm_pt_indices)} out of {sfm_pts_camera.shape[1]}"
    )
    if torch.sum(valid_sfm_pt_indices) < sfm_pts_camera.shape[1] / 4:
        raise LowDepthAlignmentConfidenceError(
            "Less than 1/4 of SFM points",
            f" ({torch.sum(valid_sfm_pt_indices).item()} / {sfm_pts_camera.shape[1]})"
            f" reprojected into image bounds.",
        )

    # Set invalid points to 0 so we can index the mask with them
    # Will be filtered out later anyways
    sfm_pts_camera[:, ~valid_sfm_pt_indices] = torch.zeros_like(
        sfm_pts_camera[:, ~valid_sfm_pt_indices]
    )
    valid_sfm_pt_indices = torch.logical_and(
        valid_sfm_pt_indices, mask[sfm_pts_camera[1], sfm_pts_camera[0]]
    )

    return sfm_pts_camera[:, valid_sfm_pt_indices], sfm_pts_camera_depth[
        valid_sfm_pt_indices
    ]


def align_depth(
    sfm_points, P, imsize, depth, mask, strategy: DepthAlignmentStrategy
) -> DepthAlignmentParams:
    device = sfm_points.device

    sfm_points_camera = P @ torch.vstack(
        [sfm_points.T, torch.ones(sfm_points.shape[0], device=device)]
    )
    sfm_points_depth = sfm_points_camera[2]
    sfm_points_camera = sfm_points_camera[:2] / sfm_points_camera[2]

    sfm_points_camera = torch.round(sfm_points_camera).to(int)
    sfm_points_camera, sfm_points_depth = get_valid_sfm_pts(
        sfm_points_camera, sfm_points_depth, mask, imsize
    )
    predicted_depth: torch.Tensor = depth[sfm_points_camera[1], sfm_points_camera[0]]
    return strategy.estimate_alignment(predicted_depth, sfm_points_depth)


def get_pts_from_depth(
    predicted_depth: PredictedDepth,
    image: torch.Tensor,
    image_name: str,
    parser: Parser | NerfbaselinesParser,
    subsampler: DepthSubsampler,
    cam2world: torch.Tensor,
    K: torch.Tensor,
    depth_alignment_strategy: DepthAlignmentStrategyEnum,
    debug_point_cloud_export_dir: Optional[Path] = None,
):
    """
    Returns:
        pts_world: torch.Tensor on depth.device of shape [N, 3] where N is the number of points in the world space
        valid_indices: torch.Tensor on depth.device of shape [N] where N is the number of points in the world space
    """
    depth = predicted_depth.depth.float()
    if predicted_depth.mask is not None:
        mask_from_predictor = predicted_depth.mask
    else:
        mask_from_predictor = torch.ones_like(predicted_depth.depth, dtype=bool)

    depth = predicted_depth.depth.float()
    imsize = depth.T.shape
    w2c = torch.linalg.inv(cam2world)
    R = w2c[:3, :3]
    C = -R.T @ w2c[:3, 3]
    P = K @ R @ torch.hstack([torch.eye(3), -C[:, None]])

    sfm_points = (
        torch.from_numpy(parser.points[parser.point_indices[image_name]])
        .to(depth.device)
        .float()
    )

    cam2world = cam2world.to(depth.device).float()
    P = P.to(depth.device).float()
    K = K.to(depth.device).float()

    def transform_camera_to_world_space(camera_homo: torch.Tensor) -> torch.Tensor:
        dense_world = torch.linalg.inv(K) @ camera_homo.reshape((-1, 3)).T
        dense_world = (
            cam2world
            @ torch.vstack(
                [dense_world, torch.ones(dense_world.shape[1], device=cam2world.device)]
            )
        )[:3].T
        return dense_world

    if torch.any(torch.isinf(depth[mask_from_predictor])):
        _LOGGER.warning("Encountered infinite depths in predicted depth map.")

    depth_alignment = align_depth(
        sfm_points,
        P,
        imsize,
        depth,
        mask_from_predictor,
        depth_alignment_strategy.get_implementation(),
    )
    aligned_depth = depth_alignment.scale * depth + depth_alignment.shift

    subsampling_mask = subsampler.get_mask(
        image, aligned_depth, mask_from_predictor
    ).cpu()

    pts_camera: torch.Tensor = (
        torch.dstack(
            [
                torch.from_numpy(np.mgrid[0 : imsize[0], 0 : imsize[1]].T).to(
                    depth.device
                ),
                aligned_depth,
            ],
        )
        .reshape(-1, 3)[subsampling_mask]
        .to(depth.device)
    )

    subsampled_mask_from_predictor = mask_from_predictor.reshape(-1)[subsampling_mask]

    pts_camera[:, 0] = (pts_camera[:, 0] + 0.5) * pts_camera[:, 2]
    pts_camera[:, 1] = (pts_camera[:, 1] + 0.5) * pts_camera[:, 2]

    pts_world_unfiltered = transform_camera_to_world_space(pts_camera)
    pts_world = pts_world_unfiltered[subsampled_mask_from_predictor]

    if debug_point_cloud_export_dir is not None:
        masked_out_world = pts_world_unfiltered[~subsampled_mask_from_predictor]
        debug_export_point_clouds(
            imsize,
            cam2world,
            P,
            transform_camera_to_world_space,
            sfm_points,
            pts_world,
            masked_out_world,
            parser,
            image_name,
            subsampling_mask.cpu(),
            image,
            debug_point_cloud_export_dir,
        )

    return (
        pts_world.reshape([-1, 3]).float(),
        subsampling_mask,
        subsampled_mask_from_predictor.cpu(),
    )
