import copy
import json
import os
import argparse
import random
import sys
import numpy as np
import torch
import math
import trimesh
from cprint import cprint
from tqdm import tqdm
from typing import List
sys.path.append("..")
from env.sampler.mk_sampler import MecKinovaSampler
from preprocessing.data_utils import check_file, compute_data_number, compute_scene_sdf
from utils.meckinova_utils import transform_trajectory_numpy
from env.scene.base_scene import Scene
from utils.io import mkdir_if_not_exists, rmdir_if_exists
from utils.transform import QuaternionXYZ2TransformationMatrix
from utils.transform import SE3

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--robot', type=str, default='MecKinova', help='robot name, such as MecKinova or Franka')
    p.add_argument('--task', type=str, default='pick', help='task name, such as pick, or place')
    p.add_argument('--data_proportion', type=List[int], default=[9, 0, 1], help='[TrainSetNum:ValSetNum:TestSetNum]')
    p.add_argument('--origin_path', type=str, help='origin data directory path')
    p.add_argument('--save_path', type=str, help='save data directory path')
    p.add_argument('--overwrite', action="store_true", help='overwrite the previous data set')

    opt = p.parse_args()
    return opt

def preprocess_data(data_path:str, save_path:str, id:int, mk_sampler:MecKinovaSampler) -> None:
    """ The data collected by VKC is preprocessed into the format used by the network.

    Args:
        data_path [str]: Data path to be processed.
        save_path [str]: Saving path of preprocessed data.
        id [int]: Starting from 0, the number of the saved data.
    ------------------------------------------------------------------------------
    Saved information: (There is currently a bug in the code for getting colors》)
    {
        'scene': {
            'name': <scene_name> (string),
            'pointcloud': {
                'points': <pc_points> (np.ndarray), # [N1, 3], scene points in agent frame
                'colors': <pc_colors> (np.ndarray), # [N1, 4]
                'description': 'local view point cloud in agent frame',
            'crop_center': (np.ndarray) # cropped rectangular center of the scene is used for data normalization
            },
            # 'sdf': {
            #     'sdf_norm_value': <sdf_norm_value> (np.ndarray), # scene normal SDF in world frame, [-1, 1]
            #     'scene_mesh_center': <scene_mesh_center> (np.ndarray), # scene mesh center value
            #     'scene_mesh_scale': <scene_mesh_scale> (np.ndarray), # scene mesh scale value
            #     'resolution': <resolution> (np.ndarray), # SDF grid resolution
            # },
        },
        'object': {
            'name': <object_name> (string),
            'pointcloud': {
                'points': <pc_points> (np.ndarray), # [N2, 3], object points in agent frame
                'colors': <pc_colors> (np.ndarray), # [N2, 4]
                'description': 'local view point cloud in agent frame',
            },
        },
        'agent': {
            'name': <agent_name> (string),
            'init_pos': <[x, y, theta]> (np.ndarray) # agent initial position in world frame
        },
        'task': {
            'name': <task_name> (string), # 'pick' or 'place',
            # If task name is 'place', 'placement_area' exists
            'grasping_pose': <grasping_pose_matrix> (np.ndarray), # [4, 4] transformation matrix, gripper frame relative to agent frame
            'placement_area': {
                'scene_placement_pc': {
                    'points': <pc_points> (np.ndarray), # [N3, 3]
                    'colors': <pc_colors> (np.ndarray), # [N3, 4]
                    'description': 'scene placement surface point cloud in agent frame',
                },
                'object_placement_pc': {
                    'points': <pc_points> (np.ndarray), # [N4, 3]
                    'colors': <pc_colors> (np.ndarray), # [N4, 4]
                    'description': 'object placement undersurface point cloud in agent frame',
                }
            }
        },
        'trajectory': {
            'traj_w': <traj_in_world_frame> (np.ndarry), # [traj_len, agent.DOF], agent trajectory in the world frame
            'traj_a': <traj_in_agent_frame> (np.ndarry), # [traj_len, agent.DOF], agent trajectory in the agent frame
            'length': <traj_len> (int), # trajectory length 
        },
        'transformation_matrix': {
            'T_aw': <tm_agent_in_world> (np.ndarry), # [4, 4] transformation matrix of agent in world frame
            'T_ow': <tm_object_in_world> (np.ndarry), # [4, 4] transformation matrix of object in world frame
            'T_oa': <tm_object_in_agent> (np.ndarry), # [4, 4] transformation matrix of object in agent frame
        },
        'object_grasping_poses': {
            'values': <tm_grasping_pose> (np.ndarry), # [N, 4, 4] grasping pose matrix
            'description': 'object grasping pose for inference selection',
        }
    }
    """

    task_name = save_path.split("/")[-2]

    ## Preprocess config.json
    with open(os.path.join(data_path, 'config.json'), "r") as f:
        config = json.load(f) # env info
        scene_name = config["env"]["scene"]["name"]
        object_name = config["env"]["object"]["name"]
        agent_name = config["env"]["agent"]["name"]
        agent_init_pos = config["env"]["agent"]["position"]
        T_ow = np.array(config["env"]["object"]["transformation_matrix"])
        grasping_pose = QuaternionXYZ2TransformationMatrix(
            config["attachments"]["given_attach_orient"], 
            config["attachments"]["given_attach_trans"]
        )

    ## Process point cloud and transformation matrix
    scene = Scene(scene_name)
    T_aw = SE3(xyz=[agent_init_pos[0], agent_init_pos[1], 0], rpy=[0, 0, agent_init_pos[2]]).matrix
    T_wa = np.linalg.inv(T_aw)
    T_oa = np.matmul(T_wa, T_ow)
    T_ao = np.linalg.inv(T_oa)
    # object points in agent frame
    scene.update_object_position_by_transformation_matrix(object_name, T_ow)
    object_pc_points = scene.get_object_points_in_scene(object_name, 2048)
    object_pc_points = trimesh.transform_points(object_pc_points, T_wa)

    # # debug
    # agent = MecKinova()
    # agent.update_config(agent_init_pos + [0,0,0,0,0,0,0])
    # visualization = trimesh.Scene()
    # visualization.add_geometry(scene.trimesh_visual)
    # visualization.add_geometry(agent.trimesh)
    # visualization.show()

    # scene points in agent frame (point clouds excluding objects)
        # phi is the angle (around the z axis) between the agent's orientation and the 
        # connection between the center of the agent and the center of the object
    phi = agent_init_pos[2] - math.atan2(T_ow[1, -1] - agent_init_pos[1], T_ow[0, -1] - agent_init_pos[0])
    T_phi = SE3(xyz=[0, 0, 0], rpy=[0, 0, -phi]).matrix
    scene.update_object_position_by_transformation_matrix(object_name, np.eye(4)) 
    scene_pc_points, scene_pc_colors = scene.crop_scene_and_sample_points(
        transformation_matrix=T_aw @ T_phi, sample_num=32768, sample_color=True, LWH=[6, 6, 2]
    )
    scene_pc_points = trimesh.transform_points(scene_pc_points, T_wa)
    # Due to cropping, the original color is lost
    # visualize_point_cloud(point_cloud=scene_pc_points, colors=scene_pc_colors[:, :-1] / 255) # visualize scene

    ## Process sdf
    if not os.path.exists(scene._sdf_path):
        sdf_dict = compute_scene_sdf(scene=scene, size=128)
        np.save(scene._sdf_path, sdf_dict)

    ## Process trajectory
    with open(os.path.join(data_path, 'trajectory', task_name + '_trajectory_absolute.json'), "r") as f:
        traj_w = json.load(f) 
    traj_a = np.array(copy.deepcopy(traj_w))
    traj_a = transform_trajectory_numpy(traj_a, T_wa, -agent_init_pos[2])
    traj_len = len(traj_w)

    ## Target end-effector point cloud
    last_config = copy.deepcopy(traj_a[-1])
    eef_pc_points = mk_sampler.sample_end_effector(torch.as_tensor(last_config), sample_points=1024)
    eef_pc_points = eef_pc_points.squeeze(0).clone().detach().cpu().numpy()

    ## Save preprocessed data
    item = {
        'scene': {
            'name': scene_name,
            'pointcloud': {
                'points': scene_pc_points, # [N1, 3]
                # 'colors': <pc_colors> (np.ndarray), # [N1, 4]
                'description': 'local view point cloud in agent frame',
            },
            # 'sdf': {
            #     'sdf_norm_value': sdf_dict['sdf_norm_value'], # scene normal SDF in world frame, [-1, 1]
            #     'scene_mesh_center': sdf_dict['scene_mesh_center'], # scene mesh center value
            #     'scene_mesh_scale': sdf_dict['scene_mesh_scale'], # scene mesh scale value
            #     'resolution': sdf_dict['resolution'], # SDF grid resolution
            # },
        },
        'object': {
            'name': object_name,
            'pointcloud': {
                'points': object_pc_points, # [N2, 3]
                # 'colors': <pc_colors> (np.ndarray), # [N2, 4]
                'description': 'local view point cloud in agent frame',
            }
        },
        'target_eef': {
            'name': 'target_end_effector',
            'pointcloud': {
                'points': eef_pc_points, # [N3, 3]
                # 'colors': <pc_colors> (np.ndarray), # [N3, 4]
                'description': 'target end-effector point cloud in agent frame',
            }
        },
        'agent': {
            'name': agent_name,
            'init_pos': agent_init_pos # agent initial position in world frame
        },
        'task': {
            'name': task_name,
            'grasping_pose': grasping_pose, # [4, 4] transformation matrix, gripper frame relative to agent frame
        },
        'trajectory': {
            'traj_w': traj_w, # [traj_len, agent.DOF], agent trajectory in the world frame
            'traj_a': traj_a, # [traj_len, agent.DOF], agent trajectory in the agent frame
            'length': traj_len, # trajectory length 
        },
        'transformation_matrix': {
            'T_aw': T_aw, # [4, 4] initial transformation matrix of agent in world frame
            'T_ow': T_ow, # [4, 4] transformation matrix of object in world frame
            'T_oa': T_oa, # [4, 4] transformation matrix of object in agent frame
        },
        # 'object_grasping_poses': {
        #     'values': Hs,
        #     'description': 'object candidate grasping poses',
        # }
    }
    np.save(os.path.join(save_path, str(id) + '.npy'), item)


if __name__ == "__main__":
    args = parse_args()

    ## set random seed
    seed = 2024
    torch.backends.cudnn.benchmark = False     
    torch.backends.cudnn.deterministic = True
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    ## Compute the number of valid data
    data_num = compute_data_number(args.origin_path, args.task)
    cprint.info("The number of {} data is {}.".format(args.task, data_num))

    ## Compute the number of training set, validation set and testing set
    data_proportion = np.array(args.data_proportion)
    tra_set_num = round(data_num * data_proportion[0] / data_proportion.sum())
    val_set_num = round(data_num * data_proportion[1] / data_proportion.sum())
    tes_set_num = data_num - tra_set_num - val_set_num
    cprint.info(
        "The number of training set, validation set and testing set is {}, {} and {}." \
            .format(tra_set_num, val_set_num, tes_set_num)
    )

    ## Slice the data set
    data_id_list = list(range(data_num))
    random.shuffle(data_id_list)
    tra_num_id = data_id_list[:tra_set_num]
    val_num_id = data_id_list[tra_set_num:tra_set_num + val_set_num]
    tes_num_id = data_id_list[-tes_set_num:]
    # cprint.info("{},{},{}".format(len(tra_num_id), len(val_num_id), len(tes_num_id)))

    ## Create directories
    tra_set_path = os.path.join(args.save_path, args.task, "train")
    val_set_path = os.path.join(args.save_path, args.task, "val")
    tes_set_path = os.path.join(args.save_path, args.task, "test")
    if args.overwrite: # if overwrite is true, remove the previous directory
        rmdir_if_exists(tra_set_path)
        rmdir_if_exists(val_set_path)
        rmdir_if_exists(tes_set_path)
    mkdir_if_not_exists(tra_set_path, True)
    mkdir_if_not_exists(val_set_path, True)
    mkdir_if_not_exists(tes_set_path, True)

    ## Create the agent sampler to accelerate kinematic computation
    mk_sampler = MecKinovaSampler('cpu', num_fixed_points=1024, use_cache=True)

    ## Make the data set
    all_num, tra_num, val_num, tes_num = 0, 0, 0, 0
    scene_iter = tqdm(os.listdir(args.origin_path), desc="{0: ^10}".format('Scene'), leave=False, mininterval=0.1)
    for scene_name in scene_iter:
        scene_dir_path = os.path.join(args.origin_path, scene_name)
        object_iter = tqdm(os.listdir(scene_dir_path), desc="{0: ^10}".format('Object'), leave=False, mininterval=0.1)
        for obj_name in object_iter:
            obj_dir_path = os.path.join(scene_dir_path, obj_name)
            timestamp_iter = tqdm(os.listdir(obj_dir_path), desc="{0: ^10}".format('TimeStamp'), leave=False, mininterval=0.1)
            for timestamp_name in timestamp_iter:
                timestamp_dir_path = os.path.join(obj_dir_path, timestamp_name)
                id_iter = tqdm(os.listdir(timestamp_dir_path), desc="{0: ^10}".format('ID'), leave=False, mininterval=0.1)
                for id_name in id_iter:
                    data_dir_path = os.path.join(timestamp_dir_path, id_name)
                    if os.path.isdir(data_dir_path):
                        if check_file("trajectory", data_dir_path) \
                        and check_file("config.json", data_dir_path) \
                        and check_file(args.task + "_vkc_return.json", data_dir_path) \
                        and check_file("vkc_request.json", data_dir_path):
                            if all_num in tra_num_id:
                                preprocess_data(data_dir_path, tra_set_path, tra_num, mk_sampler)
                                tra_num += 1
                            elif all_num in val_num_id:
                                preprocess_data(data_dir_path, val_set_path, val_num, mk_sampler)
                                val_num += 1
                            elif all_num in tes_num_id:
                                preprocess_data(data_dir_path, tes_set_path, tes_num, mk_sampler)
                                tes_num += 1
                            all_num += 1