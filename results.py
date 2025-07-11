import os

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from tqdm import tqdm

from config import config as cfg
from test_tools.common import detect_all, grab_all_frames
from test_tools.ct.operations import find_longest, multiple_tracking
from test_tools.faster_crop_align_xray import FasterCropAlignXRay
from test_tools.utils import get_crop_box
from utils.plugin_loader import PluginLoader

import csv
from datetime import datetime

mean = torch.tensor([0.485 * 255, 0.456 * 255, 0.406 * 255]).cuda().view(1, 3, 1, 1, 1)
std = torch.tensor([0.229 * 255, 0.224 * 255, 0.225 * 255]).cuda().view(1, 3, 1, 1, 1)

ROOT = r"E:\ShareID\TestDataSets\Celeb-DF-v2\laa-net_test_celeb"
FAKE_TYPE = ["Celeb-real", "Celeb-synthesis", "YouTube-real"]
EXTENSIONS = {".mp4", ".mov", ".MOV"}
max_frame = 400
#relative to ROOT
out_dir = "results"
#Relative to AltFreezing
cfg_path = "i3d_ori.yaml"
#Relative to working directory where script is launched
ckpt_path = "checkpoints/model.pth"
optimal_threshold = 0.04

def init():
    cfg.init_with_yaml()
    cfg.update_with_yaml(cfg_path)
    cfg.freeze()

def detect(video_path, crop_align_func, classifier):
    cache_file = f"{video_path}_{max_frame}.pth"

    if os.path.exists(cache_file):
        detect_res, all_lm68 = torch.load(cache_file)
        frames = grab_all_frames(video_path, max_size=max_frame, cvt=True)
        print("detection result loaded from cache")
    else:
        print("detecting")
        detect_res, all_lm68, frames = detect_all(video_path, return_frames=True, max_size=max_frame)
        torch.save((detect_res, all_lm68), cache_file)
        print("detect finished")

    print("number of frames: ", len(frames))

    shape = frames[0].shape[:2]

    all_detect_res = []

    assert len(all_lm68) == len(detect_res)

    for faces, faces_lm68 in zip(detect_res, all_lm68):
        new_faces = []
        for (box, lm5, score), face_lm68 in zip(faces, faces_lm68):
            new_face = (box, lm5, face_lm68, score)
            new_faces.append(new_face)
        all_detect_res.append(new_faces)

    detect_res = all_detect_res

    print("split into super clips")

    tracks = multiple_tracking(detect_res)
    tuples = [(0, len(detect_res))] * len(tracks)

    print("full_tracks", len(tracks))

    if len(tracks) == 0:
        tuples, tracks = find_longest(detect_res)

    data_storage = {}
    frame_boxes = {}
    super_clips = []

    for track_i, ((start, end), track) in enumerate(zip(tuples, tracks)):
        print(start, end)
        assert len(detect_res[start:end]) == len(track)

        super_clips.append(len(track))

        for face, frame_idx, j in zip(track, range(start, end), range(len(track))):
            box, lm5, lm68 = face[:3]
            big_box = get_crop_box(shape, box, scale=0.5)

            top_left = big_box[:2][None, :]
            new_lm5 = lm5 - top_left
            new_lm68 = lm68 - top_left
            new_box = (box.reshape(2, 2) - top_left).reshape(-1)
            info = (new_box, new_lm5, new_lm68, big_box)

            x1, y1, x2, y2 = big_box
            cropped = frames[frame_idx][y1:y2, x1:x2]
            base_key = f"{track_i}_{j}_"
            data_storage[f"{base_key}img"] = cropped
            data_storage[f"{base_key}ldm"] = info
            data_storage[f"{base_key}idx"] = frame_idx
            frame_boxes[frame_idx] = np.rint(box).astype(int)

    print("sampling clips from super clips", super_clips)

    clips_for_video = []
    clip_size = cfg.clip_size
    pad_length = clip_size - 1

    for super_clip_idx, super_clip_size in enumerate(super_clips):
        inner_index = list(range(super_clip_size))
        if super_clip_size < clip_size:  # padding
            post_module = inner_index[1:-1][::-1] + inner_index

            l_post = len(post_module)
            post_module = post_module * (pad_length // l_post + 1)
            post_module = post_module[:pad_length]
            assert len(post_module) == pad_length

            pre_module = inner_index + inner_index[1:-1][::-1]
            l_pre = len(pre_module)
            pre_module = pre_module * (pad_length // l_pre + 1)
            pre_module = pre_module[-pad_length:]
            assert len(pre_module) == pad_length

            inner_index = pre_module + inner_index + post_module

        super_clip_size = len(inner_index)

        #Convolution in time prep
        frame_range = [
            inner_index[i : i + clip_size] for i in range(super_clip_size) if i + clip_size <= super_clip_size
        ]

        #Clip for video [[0, 32], [0, 32 offset de 1]....]
        for indices in frame_range:
            clip = [(super_clip_idx, t) for t in indices]
            clips_for_video.append(clip)

    preds = []
    frame_res = {}

    for clip in tqdm(clips_for_video, desc="testing"):
        images = [data_storage[f"{i}_{j}_img"] for i, j in clip]
        landmarks = [data_storage[f"{i}_{j}_ldm"] for i, j in clip]
        frame_ids = [data_storage[f"{i}_{j}_idx"] for i, j in clip]
        _, images_align = crop_align_func(landmarks, images)
        for i in range(clip_size):
            img1 = cv2.resize(images[i], (cfg.imsize, cfg.imsize))
            img = np.concatenate((img1, images_align[i]), axis=1)
        images = torch.as_tensor(images_align, dtype=torch.float32).cuda().permute(3, 0, 1, 2)
        images = images.unsqueeze(0).sub(mean).div(std)  ## 输入要减去mean std

        with torch.no_grad():
            output = classifier(images)
        pred = float(F.sigmoid(output["final_output"]))
        for f_id in frame_ids:
            if f_id not in frame_res:
                frame_res[f_id] = []
            frame_res[f_id].append(pred)
        preds.append(pred)
    print(np.mean(preds))

    pred_probs = []
    for frame_idx in range(len(frames)):
        if frame_idx in frame_res:
            pred_probs.append(np.mean(frame_res[frame_idx]))

    return pred_probs
    
def save_as_csv(vid_probs):
    res_dir = os.path.join(ROOT, out_dir)
    os.makedirs(res_dir, exist_ok=True)
    now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_altfreezing")
    file_name = os.path.join(res_dir, now_str) + '.csv'
    # Find max number of values to create header dynamically
    max_len = max(len(v) for v in vid_probs.values())

    # Save to CSV
    with open(file_name, "w", newline='') as f:
        writer = csv.writer(f, delimiter=';')
        header = ["ID"] + [f"value_{i}" for i in range(max_len)]
        writer.writerow(header)

        for key, values in vid_probs.items():
            # Pad with empty strings if values are shorter
            padded_values = values + [""] * (max_len - len(values))
            writer.writerow([key] + padded_values)

if __name__ == "__main__":
    init()
    classifier = PluginLoader.get_classifier(cfg.classifier_type)()
    classifier.cuda()
    classifier.eval()
    classifier.load(ckpt_path)
    crop_align_func = FasterCropAlignXRay(cfg.imsize)
    vid_preds = {} 
    counter = 0
    for fake_type in FAKE_TYPE:
        fake_type_path = os.path.join(ROOT, fake_type)
        for file in os.listdir(fake_type_path):
            (vid_name, extension) = os.path.splitext(file)
            if extension in EXTENSIONS:
                counter +=  1
                video_path = os.path.join(ROOT, fake_type, file)
                print(f"-------------{file}-{counter}----------------")
                pred_probs = detect(video_path, crop_align_func, classifier)
                vid_preds[vid_name] = pred_probs
    save_as_csv(vid_preds)
