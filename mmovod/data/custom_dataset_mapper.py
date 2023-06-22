# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import copy
import logging
import numpy as np
from typing import List, Optional, Union
import torch
import pycocotools.mask as mask_util

from detectron2.config import configurable

from detectron2.data import detection_utils as utils
from detectron2.data.detection_utils import transform_keypoint_annotations
from detectron2.data import transforms as T
from detectron2.data.dataset_mapper import DatasetMapper
from detectron2.structures import Boxes, BoxMode, Instances
from detectron2.structures import Keypoints, PolygonMasks, BitMasks
from fvcore.transforms.transform import TransformList
from .custom_build_augmentation import build_custom_augmentation

__all__ = ["CustomDatasetMapper"]

class CustomDatasetMapper(DatasetMapper):
    @configurable
    def __init__(self, is_train: bool, 
        with_ann_type=False,
        dataset_ann=[],
        use_diff_bs_size=False,
        dataset_augs=[],
        is_debug=False,
        use_tar_dataset=False,
        tarfile_path='',
        tar_index_dir='',
        **kwargs):
        """
        add image labels
        """
        self.with_ann_type = with_ann_type
        self.dataset_ann = dataset_ann
        self.use_diff_bs_size = use_diff_bs_size
        if self.use_diff_bs_size and is_train:
            self.dataset_augs = [T.AugmentationList(x) for x in dataset_augs]
        self.is_debug = is_debug
        self.use_tar_dataset = use_tar_dataset
        if self.use_tar_dataset:
            raise NotImplementedError('Using tar dataset not supported')
        super().__init__(is_train, **kwargs)
 

    @classmethod
    def from_config(cls, cfg, is_train: bool = True):
        ret = super().from_config(cfg, is_train)
        ret.update({
            'with_ann_type': cfg.WITH_IMAGE_LABELS,
            'dataset_ann': cfg.DATALOADER.DATASET_ANN,
            'use_diff_bs_size': cfg.DATALOADER.USE_DIFF_BS_SIZE,
            'is_debug': cfg.IS_DEBUG,
            'use_tar_dataset': cfg.DATALOADER.USE_TAR_DATASET,
            'tarfile_path': cfg.DATALOADER.TARFILE_PATH,
            'tar_index_dir': cfg.DATALOADER.TAR_INDEX_DIR,
        })
        if ret['use_diff_bs_size'] and is_train:
            if cfg.INPUT.CUSTOM_AUG == 'EfficientDetResizeCrop':
                dataset_scales = cfg.DATALOADER.DATASET_INPUT_SCALE
                dataset_sizes = cfg.DATALOADER.DATASET_INPUT_SIZE
                ret['dataset_augs'] = [
                    build_custom_augmentation(cfg, True, scale, size) \
                        for scale, size in zip(dataset_scales, dataset_sizes)]
            else:
                assert cfg.INPUT.CUSTOM_AUG == 'ResizeShortestEdge'
                min_sizes = cfg.DATALOADER.DATASET_MIN_SIZES
                max_sizes = cfg.DATALOADER.DATASET_MAX_SIZES
                ret['dataset_augs'] = [
                    build_custom_augmentation(
                        cfg, True, min_size=mi, max_size=ma) \
                        for mi, ma in zip(min_sizes, max_sizes)]
        else:
            ret['dataset_augs'] = []

        return ret

    def __call__(self, dataset_dict):
        """
        include image labels
        """
        dataset_dict = copy.deepcopy(dataset_dict)  # it will be modified by code below
        # USER: Write your own image loading if it's not from a file
        if 'file_name' in dataset_dict:
            ori_image = utils.read_image(
                dataset_dict["file_name"], format=self.image_format)
        else:
            ori_image, _, _ = self.tar_dataset[dataset_dict["tar_index"]]
            ori_image = utils._apply_exif_orientation(ori_image)
            ori_image = utils.convert_PIL_to_numpy(ori_image, self.image_format)
        utils.check_image_size(dataset_dict, ori_image)

        # USER: Remove if you don't do semantic/panoptic segmentation.
        if "sem_seg_file_name" in dataset_dict:
            sem_seg_gt = utils.read_image(
                dataset_dict.pop("sem_seg_file_name"), "L").squeeze(2)
        else:
            sem_seg_gt = None

        if self.is_debug:
            dataset_dict['dataset_source'] = 0

        not_full_labeled = 'dataset_source' in dataset_dict and \
            self.with_ann_type and \
                self.dataset_ann[dataset_dict['dataset_source']] != 'box'

        aug_input = T.AugInput(copy.deepcopy(ori_image), sem_seg=sem_seg_gt)
        if self.use_diff_bs_size and self.is_train:
            transforms = \
                self.dataset_augs[dataset_dict['dataset_source']](aug_input)
        else:
            transforms = self.augmentations(aug_input)
        image, sem_seg_gt = aug_input.image, aug_input.sem_seg

        image_shape = image.shape[:2]  # h, w
        dataset_dict["image"] = torch.as_tensor(
            np.ascontiguousarray(image.transpose(2, 0, 1)))
        
        if sem_seg_gt is not None:
            dataset_dict["sem_seg"] = torch.as_tensor(sem_seg_gt.astype("long"))

        # USER: Remove if you don't use pre-computed proposals.
        # Most users would not need this feature.
        if self.proposal_topk is not None:
            utils.transform_proposals(
                dataset_dict, image_shape, transforms, 
                proposal_topk=self.proposal_topk
            )

        if not self.is_train:
            # USER: Modify this if you want to keep them for some reason.
            dataset_dict.pop("annotations", None)
            dataset_dict.pop("sem_seg_file_name", None)
            return dataset_dict

        if "annotations" in dataset_dict:
            # USER: Modify this if you want to keep them for some reason.
            for anno in dataset_dict["annotations"]:
                if not self.use_instance_mask:
                    anno.pop("segmentation", None)
                if not self.use_keypoint:
                    anno.pop("keypoints", None)

            # USER: Implement additional transformations if you have other types of data
            all_annos = [
                (utils.transform_instance_annotations(
                    obj, transforms, image_shape, 
                    keypoint_hflip_indices=self.keypoint_hflip_indices,
                ),  obj.get("iscrowd", 0))
                for obj in dataset_dict.pop("annotations")
            ]
            annos = [ann[0] for ann in all_annos if ann[1] == 0]
            instances = utils.annotations_to_instances(
                annos, image_shape, mask_format=self.instance_mask_format
            )
            
            del all_annos
            if self.recompute_boxes:
                instances.gt_boxes = instances.gt_masks.get_bounding_boxes()
            dataset_dict["instances"] = utils.filter_empty_instances(instances)
        if self.with_ann_type:
            dataset_dict["pos_category_ids"] = dataset_dict.get(
                'pos_category_ids', [])
            dataset_dict["ann_type"] = \
                self.dataset_ann[dataset_dict['dataset_source']]
        if self.is_debug and (('pos_category_ids' not in dataset_dict) or \
            (dataset_dict['pos_category_ids'] == [])):
            dataset_dict['pos_category_ids'] = [x for x in sorted(set(
                dataset_dict['instances'].gt_classes.tolist()
            ))]
        return dataset_dict
