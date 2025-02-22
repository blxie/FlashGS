import torch
import flash_gaussian_splatting

import os
import json
import time
import argparse


class Scene:
    def __init__(self, device):
        self.device = device
        self.num_vertex = 0
        self.position = None
        self.shs = None
        self.opacity = None
        self.cov3d = None

    def loadPly(self, scene_path):
        self.num_vertex, self.position, self.shs, self.opacity, self.cov3d = flash_gaussian_splatting.ops.loadPly(
            scene_path)
        print("num_vertex = %d" % self.num_vertex)
        # 58*4byte
        self.position = self.position.to(self.device)  # 3
        self.shs = self.shs.to(self.device)  # 48
        self.opacity = self.opacity.to(self.device)  # 1
        self.cov3d = self.cov3d.to(self.device)  # 6


class Camera:
    def __init__(self, camera_json, resolution=None):
        self.id = camera_json['id']
        self.img_name = camera_json['img_name']

        if resolution:
            self.width, self.height = resolution
        else:
            self.width, self.height = camera_json['width'], camera_json['height']

        self.width_from_json = camera_json['width']
        self.height_from_json = camera_json['height']

        self.position = torch.tensor(camera_json['position'])
        self.rotation = torch.tensor(camera_json['rotation'])
        self.focal_x = camera_json['fx']
        self.focal_y = camera_json['fy']
        self.zFar = 100.0
        self.zNear = 0.01


# 静态分配内存光栅化器
class Rasterizer:
    # 构造函数中分配内存
    def __init__(self, scene, MAX_NUM_RENDERED, MAX_NUM_TILES):
        # 24 bytes
        self.gaussian_keys_unsorted = torch.zeros(MAX_NUM_RENDERED, device=scene.device, dtype=torch.int64)
        self.gaussian_values_unsorted = torch.zeros(MAX_NUM_RENDERED, device=scene.device, dtype=torch.int32)
        self.gaussian_keys_sorted = torch.zeros(MAX_NUM_RENDERED, device=scene.device, dtype=torch.int64)
        self.gaussian_values_sorted = torch.zeros(MAX_NUM_RENDERED, device=scene.device, dtype=torch.int32)

        self.MAX_NUM_RENDERED = MAX_NUM_RENDERED
        self.MAX_NUM_TILES = MAX_NUM_TILES
        self.SORT_BUFFER_SIZE = flash_gaussian_splatting.ops.get_sort_buffer_size(MAX_NUM_RENDERED)
        self.list_sorting_space = torch.zeros(self.SORT_BUFFER_SIZE, device=scene.device, dtype=torch.int8)
        self.ranges = torch.zeros((MAX_NUM_TILES, 2), device=scene.device, dtype=torch.int32)
        self.curr_offset = torch.zeros(1, device=scene.device, dtype=torch.int32)

        # 40 bytes
        self.points_xy = torch.zeros((scene.num_vertex, 2), device=scene.device, dtype=torch.float32)
        self.rgb_depth = torch.zeros((scene.num_vertex, 4), device=scene.device, dtype=torch.float32)
        self.conic_opacity = torch.zeros((scene.num_vertex, 4), device=scene.device, dtype=torch.float32)

    # 前向传播（应用层封装）
    def forward(self, scene, camera, bg_color):
        # 属性预处理 + 键值绑定
        self.curr_offset.fill_(0)
        flash_gaussian_splatting.ops.preprocess(scene.position, scene.shs, scene.opacity, scene.cov3d,
                                                camera.width, camera.height, 16, 16,
                                                camera.width_from_json, camera.height_from_json,
                                                camera.position, camera.rotation,
                                                camera.focal_x, camera.focal_y, camera.zFar, camera.zNear,
                                                self.points_xy, self.rgb_depth, self.conic_opacity,
                                                self.gaussian_keys_unsorted, self.gaussian_values_unsorted,
                                                self.curr_offset)

        # 键值对数量判断 + 处理键值对过多的异常情况
        num_rendered = int(self.curr_offset.cpu()[0])
        # print(num_rendered)
        if num_rendered >= self.MAX_NUM_RENDERED:
            raise "Too many k-v pairs!"

        flash_gaussian_splatting.ops.sort_gaussian(num_rendered, camera.width, camera.height, 16, 16,
                                                   self.list_sorting_space,
                                                   self.gaussian_keys_unsorted, self.gaussian_values_unsorted,
                                                   self.gaussian_keys_sorted, self.gaussian_values_sorted)
        # 排序 + 像素着色 + 混色阶段
        out_color = torch.zeros((camera.height, camera.width, 3), device=scene.device, dtype=torch.int8)
        flash_gaussian_splatting.ops.render_16x16(num_rendered, camera.width, camera.height,
                                                  self.points_xy, self.rgb_depth, self.conic_opacity,
                                                  self.gaussian_keys_sorted, self.gaussian_values_sorted,
                                                  self.ranges, bg_color, out_color)
        return out_color


def savePpm(image, path):
    image = image.cpu()
    assert image.dim() >= 3
    assert image.size(2) == 3
    with open(path, 'wb') as f:
        f.write(b'P6\n' + f'{image.size(1)} {image.size(0)}\n255\n'.encode() + image.numpy().tobytes())


def render_scene(model_path, test_performance=False, **kwargs):
    scene_path = os.path.join(model_path, "point_cloud", "iteration_30000", "point_cloud.ply")
    print(scene_path)
    camera_path = os.path.join(model_path, "cameras.json")
    print(camera_path)
    device = torch.device('cuda:0')
    bg_color = torch.zeros(3, dtype=torch.float32)  # black

    scene = Scene(device)
    scene.loadPly(scene_path)

    with open(camera_path, 'r') as camera_file:
        cameras_json = json.loads(camera_file.read())

    image_dir = os.path.join(model_path, "test_out")
    if not os.path.exists(image_dir):
        os.mkdir(image_dir)

    MAX_NUM_RENDERED = 2 ** 27
    MAX_NUM_TILES = 2 ** 20
    rasterizer = Rasterizer(scene, MAX_NUM_RENDERED, MAX_NUM_TILES)
    for camera_json in cameras_json:
        camera = Camera(camera_json, resolution=kwargs.get("resolution"))
        print("image name = %s" % camera.img_name)

        image = rasterizer.forward(scene, camera, bg_color)  # warm up

        if test_performance:
            n = 10
            torch.cuda.synchronize()
            t0 = time.time()
            for _ in range(n):
                image = rasterizer.forward(scene, camera, bg_color)  # test performance
            torch.cuda.synchronize()
            t1 = time.time()
            print("elapsed time = %f ms" % ((t1 - t0) / n * 1000))
            print("fps = %f" % (n / (t1 - t0)))

        image_path = os.path.join(image_dir, "%s.ppm" % camera.img_name)
        savePpm(image, image_path)


def parse_resolution(resolution_str):
    try:
        width, height = map(int, resolution_str.split('x'))
        return (width, height)
    except:
        raise argparse.ArgumentTypeError('Resolution must be in format WIDTHxHEIGHT (e.g. 1920x1080)')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='3D Gaussian Splatting Renderer')
    parser.add_argument('--model', type=str, help='Path to single model')
    parser.add_argument('--models_dir', type=str, default="./models",
                         help='Directory containing multiple models')
    parser.add_argument('--resolution', type=parse_resolution,
                         help='Custom resolution in format WIDTHxHEIGHT (e.g. 1920x1080)')

    args = parser.parse_args()

    if args.model:
        render_scene(args.model, True, resolution=args.resolution)
    else:
        for entry in os.scandir(args.models_dir):
            if entry.is_dir():
                render_scene(entry.path, resolution=args.resolution)
