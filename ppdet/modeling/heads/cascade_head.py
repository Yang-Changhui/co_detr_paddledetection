# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved. 
#   
# Licensed under the Apache License, Version 2.0 (the "License");   
# you may not use this file except in compliance with the License.  
# You may obtain a copy of the License at   
#   
#     http://www.apache.org/licenses/LICENSE-2.0    
#   
# Unless required by applicable law or agreed to in writing, software   
# distributed under the License is distributed on an "AS IS" BASIS, 
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  
# See the License for the specific language governing permissions and   
# limitations under the License.

import paddle
import paddle.nn as nn
import paddle.nn.functional as F
from paddle.nn.initializer import Normal, XavierUniform
from paddle.regularizer import L2Decay

from ppdet.core.workspace import register, create
from ppdet.modeling import ops

from .bbox_head import BBoxHead, TwoFCHead, XConvNormHead
from .roi_extractor import RoIAlign
from ..shape_spec import ShapeSpec
from ..bbox_utils import bbox2delta, delta2bbox, clip_bbox, nonempty_bbox

__all__ = ['CascadeTwoFCHead', 'CascadeXConvNormHead', 'CascadeHead']


@register
class CascadeTwoFCHead(nn.Layer):
    __shared__ = ['num_cascade_stage']

    def __init__(self,
                 in_dim=256,
                 mlp_dim=1024,
                 resolution=7,
                 num_cascade_stage=3):
        super(CascadeTwoFCHead, self).__init__()

        self.in_dim = in_dim
        self.mlp_dim = mlp_dim

        self.head_list = []
        for stage in range(num_cascade_stage):
            head_per_stage = self.add_sublayer(
                str(stage), TwoFCHead(in_dim, mlp_dim, resolution))
            self.head_list.append(head_per_stage)

    @classmethod
    def from_config(cls, cfg, input_shape):
        s = input_shape
        s = s[0] if isinstance(s, (list, tuple)) else s
        return {'in_dim': s.channels}

    @property
    def out_shape(self):
        return [ShapeSpec(channels=self.mlp_dim, )]

    def forward(self, rois_feat, stage=0):
        out = self.head_list[stage](rois_feat)
        return out


@register
class CascadeXConvNormHead(nn.Layer):
    __shared__ = ['norm_type', 'freeze_norm', 'num_cascade_stage']

    def __init__(self,
                 in_dim=256,
                 num_convs=4,
                 conv_dim=256,
                 mlp_dim=1024,
                 resolution=7,
                 norm_type='gn',
                 freeze_norm=False,
                 num_cascade_stage=3):
        super(CascadeXConvNormHead, self).__init__()
        self.in_dim = in_dim
        self.mlp_dim = mlp_dim

        self.head_list = []
        for stage in range(num_cascade_stage):
            head_per_stage = self.add_sublayer(
                str(stage),
                XConvNormHead(
                    in_dim,
                    num_convs,
                    conv_dim,
                    mlp_dim,
                    resolution,
                    norm_type,
                    freeze_norm,
                    stage_name='stage{}_'.format(stage)))
            self.head_list.append(head_per_stage)

    @classmethod
    def from_config(cls, cfg, input_shape):
        s = input_shape
        s = s[0] if isinstance(s, (list, tuple)) else s
        return {'in_dim': s.channels}

    @property
    def out_shape(self):
        return [ShapeSpec(channels=self.mlp_dim, )]

    def forward(self, rois_feat, stage=0):
        out = self.head_list[stage](rois_feat)
        return out


@register
class CascadeHead(BBoxHead):
    __shared__ = ['num_classes', 'num_cascade_stages']
    __inject__ = ['bbox_assigner']
    """
    head (nn.Layer): Extract feature in bbox head
    in_channel (int): Input channel after RoI extractor
    roi_extractor (object): The module of RoI Extractor
    bbox_assigner (object): The module of Box Assigner, label and sample the 
                            box.
    num_classes (int): The number of classes
    bbox_weight (List[List[float]]): The weight to get the decode box and the 
                                     length of weight is the number of cascade 
                                     stage
    num_cascade_stages (int): THe number of stage to refine the box
    """

    def __init__(self,
                 head,
                 in_channel,
                 roi_extractor=RoIAlign().__dict__,
                 bbox_assigner='BboxAssigner',
                 num_classes=80,
                 bbox_weight=[[10., 10., 5., 5.], [20.0, 20.0, 10.0, 10.0],
                              [30.0, 30.0, 15.0, 15.0]],
                 num_cascade_stages=3):
        nn.Layer.__init__(self, )
        self.head = head
        self.roi_extractor = roi_extractor
        if isinstance(roi_extractor, dict):
            self.roi_extractor = RoIAlign(**roi_extractor)
        self.bbox_assigner = bbox_assigner

        self.num_classes = num_classes
        self.bbox_weight = bbox_weight
        self.num_cascade_stages = num_cascade_stages

        self.bbox_score_list = []
        self.bbox_delta_list = []
        for i in range(num_cascade_stages):
            score_name = 'bbox_score_stage{}'.format(i)
            delta_name = 'bbox_delta_stage{}'.format(i)
            bbox_score = self.add_sublayer(
                score_name,
                nn.Linear(
                    in_channel,
                    self.num_classes + 1,
                    weight_attr=paddle.ParamAttr(initializer=Normal(
                        mean=0.0, std=0.01))))

            bbox_delta = self.add_sublayer(
                delta_name,
                nn.Linear(
                    in_channel,
                    4,
                    weight_attr=paddle.ParamAttr(initializer=Normal(
                        mean=0.0, std=0.001))))
            self.bbox_score_list.append(bbox_score)
            self.bbox_delta_list.append(bbox_delta)
        self.assigned_label = None
        self.assigned_rois = None

    def forward(self, body_feats=None, rois=None, rois_num=None, inputs=None):
        """
        body_feats (list[Tensor]): Feature maps from backbone
        rois (Tensor): RoIs generated from RPN module
        rois_num (Tensor): The number of RoIs in each image
        inputs (dict{Tensor}): The ground-truth of image
        """
        targets = []
        if self.training:
            rois, rois_num, targets = self.bbox_assigner(rois, rois_num, inputs)
            targets_list = [targets]
            self.assigned_rois = (rois, rois_num)
            self.assigned_targets = targets

        pred_bbox = None
        head_out_list = []
        for i in range(self.num_cascade_stages):
            if i > 0:
                rois, rois_num = self._get_rois_from_boxes(pred_bbox,
                                                           inputs['im_shape'])
                if self.training:
                    rois, rois_num, targets = self.bbox_assigner(
                        rois, rois_num, inputs, i, is_cascade=True)
                    targets_list.append(targets)

            rois_feat = self.roi_extractor(body_feats, rois, rois_num)
            bbox_feat = self.head(rois_feat, i)
            scores = self.bbox_score_list[i](bbox_feat)
            deltas = self.bbox_delta_list[i](bbox_feat)
            head_out_list.append([scores, deltas, rois])
            pred_bbox = self._get_pred_bbox(deltas, rois, self.bbox_weight[i])

        if self.training:
            loss = {}
            for stage, value in enumerate(zip(head_out_list, targets_list)):
                (scores, deltas, rois), targets = value
                loss_stage = self.get_loss(scores, deltas, targets, rois,
                                           self.bbox_weight[stage])
                for k, v in loss_stage.items():
                    loss[k + "_stage{}".format(
                        stage)] = v / self.num_cascade_stages

            return loss, bbox_feat
        else:
            scores, deltas, self.refined_rois = self.get_prediction(
                head_out_list)
            return (deltas, scores), self.head

    def _get_rois_from_boxes(self, boxes, im_shape):
        rois = []
        for i, boxes_per_image in enumerate(boxes):
            clip_box = clip_bbox(boxes_per_image, im_shape[i])
            if self.training:
                keep = nonempty_bbox(clip_box)
                if keep.shape[0] == 0:
                    keep = paddle.zeros([1], dtype='int32')
                clip_box = paddle.gather(clip_box, keep)
            rois.append(clip_box)
        rois_num = paddle.concat([paddle.shape(r)[0] for r in rois])
        return rois, rois_num

    def _get_pred_bbox(self, deltas, proposals, weights):
        pred_proposals = paddle.concat(proposals) if len(
            proposals) > 1 else proposals[0]
        pred_bbox = delta2bbox(deltas, pred_proposals, weights)
        pred_bbox = paddle.reshape(pred_bbox, [-1, deltas.shape[-1]])
        num_prop = [p.shape[0] for p in proposals]
        return pred_bbox.split(num_prop)

    def get_prediction(self, head_out_list):
        """
        head_out_list(List[Tensor]): scores, deltas, rois
        """
        pred_list = []
        scores_list = [F.softmax(head[0]) for head in head_out_list]
        scores = paddle.add_n(scores_list) / self.num_cascade_stages
        # Get deltas and rois from the last stage
        _, deltas, rois = head_out_list[-1]
        return scores, deltas, rois

    def get_refined_rois(self, ):
        return self.refined_rois