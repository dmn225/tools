from __future__ import annotations

import math
import torch
import torch.nn as nn
import torch.nn.functional as F


def make_anchors(feats, strides, grid_cell_offset=0.5):
    """Generate anchors from features."""
    anchor_points, stride_tensor = [], []
    assert feats is not None
    dtype, device = feats[0].dtype, feats[0].device
    for i, stride in enumerate(strides):
        _, _, h, w = feats[i].shape
        sx = torch.arange(end=w, device=device, dtype=dtype) + grid_cell_offset  # shift x
        sy = torch.arange(end=h, device=device, dtype=dtype) + grid_cell_offset  # shift y
        sy, sx = torch.meshgrid(sy, sx, indexing="ij")
        anchor_points.append(torch.stack((sx, sy), -1).view(-1, 2))
        stride_tensor.append(torch.full((h * w, 1), stride, dtype=dtype, device=device))
    return torch.cat(anchor_points), torch.cat(stride_tensor)


class DetectV5(nn.Module):
    '''
    YOLOv5 Detect head for detection models.
    '''
    def __init__(self, old_detect):
        super().__init__()
        self.nc = old_detect.nc  # number of classes
        self.no = old_detect.no  # number of outputs per anchor
        self.nl = old_detect.nl  # number of detection layers
        self.na = old_detect.na
        self.grid = old_detect.grid # [torch.zeros(1)] * self.nl
        self.anchor_grid = old_detect.anchor_grid
        self.m = old_detect.m
        self.inplace = old_detect.inplace
        self.stride = old_detect.stride
        self.anchors = old_detect.anchors
        self.f = old_detect.f
        self.i = old_detect.i

    def forward(self, x):
        outputs = []

        for i in range(self.nl):
            x[i] = self.m[i](x[i])  # conv
            channel_output = torch.sigmoid(x[i])
            outputs.append(channel_output)
        
        return outputs


class DetectV7(nn.Module):
    '''
    YOLOv7 Detect head for detection models.
    '''
    def __init__(self, old_detect):
        super().__init__()
        self.nc = old_detect.nc  # number of classes
        self.no = old_detect.no  # number of outputs per anchor
        self.nl = old_detect.nl  # number of detection layers
        self.na = old_detect.na
        self.grid = old_detect.grid
        self.anchor_grid = old_detect.anchor_grid
        self.m = old_detect.m
        self.stride = old_detect.stride
        self.anchors = old_detect.anchors
        self.f = old_detect.f
        self.i = old_detect.i

    def forward(self, x):
        outputs = []

        for i in range(self.nl):
            x[i] = self.m[i](x[i])  # conv
            channel_output = torch.sigmoid(x[i])
            outputs.append(channel_output)
        
        return outputs


class DetectV6R1(nn.Module):
    '''Efficient Decoupled Head
    With hardware-aware degisn, the decoupled head is optimized with
    hybridchannels methods.
    '''
    # def __init__(self, num_classes=80, anchors=1, num_layers=3, inplace=True, head_layers=None, use_dfl=True, reg_max=16):  # detection layer
    def __init__(self, old_detect):  # detection layer
        super().__init__()
        self.nc = old_detect.nc  # number of classes
        self.no = old_detect.no  # number of outputs per anchor
        self.nl = old_detect.nl  # number of detection layers
        self.na = old_detect.na
        self.anchors = old_detect.anchors
        self.grid = old_detect.grid # [torch.zeros(1)] * self.nl
        self.prior_prob = 1e-2
        self.inplace = old_detect.inplace
        stride = [8, 16, 32]  # strides computed during build
        self.stride = torch.tensor(stride)

        # Init decouple head
        self.stems = old_detect.stems
        self.cls_convs = old_detect.cls_convs
        self.reg_convs = old_detect.reg_convs
        self.cls_preds = old_detect.cls_preds
        self.reg_preds = old_detect.reg_preds
        # New
        self.obj_preds = old_detect.obj_preds

    def forward(self, x):
        z = []
        for i in range(self.nl):
            x[i] = self.stems[i](x[i])
            cls_x = x[i]
            reg_x = x[i]
            cls_feat = self.cls_convs[i](cls_x)
            cls_output = self.cls_preds[i](cls_feat)
            reg_feat = self.reg_convs[i](reg_x)
            reg_output = self.reg_preds[i](reg_feat)
            obj_output = self.obj_preds[i](reg_feat)
            y = torch.cat([reg_output, obj_output.sigmoid(), cls_output.sigmoid()], 1)
            bs, _, ny, nx = y.shape
            y = y.view(bs, self.na, self.no, ny, nx).permute(0, 1, 3, 4, 2).contiguous()
            if self.grid[i].shape[2:4] != y.shape[2:4]:
                d = self.stride.device
                yv, xv = torch.meshgrid([torch.arange(ny).to(d), torch.arange(nx).to(d)])
                self.grid[i] = torch.stack((xv, yv), 2).view(1, self.na, ny, nx, 2).float()
            if self.inplace:
                y[..., 0:2] = (y[..., 0:2] + self.grid[i]) * self.stride[i]  # xy
                y[..., 2:4] = torch.exp(y[..., 2:4]) * self.stride[i] # wh
            else:
                xy = (y[..., 0:2] + self.grid[i]) * self.stride[i]  # xy
                wh = torch.exp(y[..., 2:4]) * self.stride[i]  # wh
                y = torch.cat((xy, wh, y[..., 4:]), -1)
            z.append(y.view(bs, -1, self.no))
        return torch.cat(z, 1)


class DetectV6R3(nn.Module):
    """Efficient Decoupled Head for YOLOv6 R2&R3 With hardware-aware degisn, the
    decoupled head is optimized with hybridchannels methods."""

    def __init__(self, old_detect, use_rvc2: bool):  # detection layer
        super().__init__()
        self.nc = old_detect.nc  # number of classes
        self.no = old_detect.no  # number of outputs per anchor
        self.nl = old_detect.nl  # number of detection layers
        if hasattr(old_detect, "anchors"):
            self.anchors = old_detect.anchors
        self.grid = old_detect.grid  # [torch.zeros(1)] * self.nl
        self.prior_prob = 1e-2
        self.inplace = old_detect.inplace
        stride = [8, 16, 32]  # strides computed during build
        self.stride = torch.tensor(stride)
        self.use_dfl = old_detect.use_dfl
        self.reg_max = old_detect.reg_max
        self.proj_conv = old_detect.proj_conv
        self.grid_cell_offset = 0.5
        self.grid_cell_size = 5.0

        # Init decouple head
        self.stems = old_detect.stems
        self.cls_convs = old_detect.cls_convs
        self.reg_convs = old_detect.reg_convs
        if hasattr(old_detect, "cls_preds"):
            self.cls_preds = old_detect.cls_preds
        elif hasattr(old_detect, "cls_preds_af"):
            self.cls_preds = old_detect.cls_preds_af
        if hasattr(old_detect, "reg_preds"):
            self.reg_preds = old_detect.reg_preds
        elif hasattr(old_detect, "reg_preds_af"):
            self.reg_preds = old_detect.reg_preds_af

        self.use_rvc2 = use_rvc2

    def forward(self, x):
        outputs = []
        for i in range(self.nl):
            b, _, h, w = x[i].shape
            l = h * w
            x[i] = self.stems[i](x[i])
            cls_x = x[i]
            reg_x = x[i]
            cls_feat = self.cls_convs[i](cls_x)
            cls_output = self.cls_preds[i](cls_feat)
            reg_feat = self.reg_convs[i](reg_x)
            reg_output = self.reg_preds[i](reg_feat)

            if self.use_dfl:
                reg_output = reg_output.reshape([-1, 4, self.reg_max + 1, l]).permute(
                    0, 2, 1, 3
                )
                reg_output = self.proj_conv(F.softmax(reg_output, dim=1))[:, 0]
                reg_output = reg_output.reshape([-1, 4, h, w])

            cls_output = torch.sigmoid(cls_output)
            # conf, _ = cls_output.max(1, keepdim=True)
            if self.use_rvc2:
                conf, _ = cls_output.max(1, keepdim=True)
            else:
                conf = torch.ones(
                    (cls_output.shape[0], 1, cls_output.shape[2], cls_output.shape[3]),
                    device=cls_output.device,
                )
            output = torch.cat([reg_output, conf, cls_output], axis=1)
            outputs.append(output)

        return outputs


class DetectV6R4s(nn.Module):
    """Efficient Decoupled Head for YOLOv6 R4 nano & small With hardware-aware design,
    the decoupled head is optimized with hybridchannels methods."""

    def __init__(self, old_detect, use_rvc2: bool):  # detection layer
        super().__init__()
        self.nc = old_detect.nc  # number of classes
        self.no = old_detect.no  # number of outputs per anchor
        self.nl = old_detect.nl  # number of detection layers
        if hasattr(old_detect, "anchors"):
            self.anchors = old_detect.anchors
        self.grid = old_detect.grid  # [torch.zeros(1)] * self.nl
        self.prior_prob = 1e-2
        self.inplace = old_detect.inplace
        self.stride = old_detect.stride
        if hasattr(old_detect, "use_dfl"):
            self.use_dfl = old_detect.use_dfl
            # print(old_detect.use_dfl)
        if hasattr(old_detect, "reg_max"):
            self.reg_max = old_detect.reg_max
        if hasattr(old_detect, "proj_conv"):
            self.proj_conv = old_detect.proj_conv
        self.grid_cell_offset = 0.5
        self.grid_cell_size = 5.0

        # Init decouple head
        self.stems = old_detect.stems
        self.cls_convs = old_detect.cls_convs
        self.reg_convs = old_detect.reg_convs
        if hasattr(old_detect, "cls_preds"):
            self.cls_preds = old_detect.cls_preds
        elif hasattr(old_detect, "cls_preds_af"):
            self.cls_preds = old_detect.cls_preds_af
        if hasattr(old_detect, "reg_preds"):
            self.reg_preds = old_detect.reg_preds
        elif hasattr(old_detect, "reg_preds_af"):
            self.reg_preds = old_detect.reg_preds_af

        self.use_rvc2 = use_rvc2

    def forward(self, x):
        outputs = []

        for i in range(self.nl):
            b, _, h, w = x[i].shape
            l = h * w
            x[i] = self.stems[i](x[i])
            cls_x = x[i]
            reg_x = x[i]
            cls_feat = self.cls_convs[i](cls_x)
            cls_output = self.cls_preds[i](cls_feat)
            reg_feat = self.reg_convs[i](reg_x)
            reg_output = self.reg_preds[i](reg_feat)

            cls_output = torch.sigmoid(cls_output)

            if self.use_rvc2:
                conf, _ = cls_output.max(1, keepdim=True)
            else:
                conf = torch.ones(
                    (cls_output.shape[0], 1, cls_output.shape[2], cls_output.shape[3]),
                    device=cls_output.device,
                )
            output = torch.cat([reg_output, conf, cls_output], axis=1)
            outputs.append(output)

        return outputs


class DetectV6R4m(nn.Module):
    """Efficient Decoupled Head for YOLOv6 R4 medium & large With hardware-aware design,
    the decoupled head is optimized with hybridchannels methods."""

    def __init__(self, old_detect, use_rvc2: bool):  # detection layer
        super().__init__()
        self.nc = old_detect.nc  # number of classes
        self.no = old_detect.no  # number of outputs per anchor
        self.nl = old_detect.nl  # number of detection layers
        if hasattr(old_detect, "anchors"):
            self.anchors = old_detect.anchors
        self.grid = old_detect.grid  # [torch.zeros(1)] * self.nl
        self.prior_prob = 1e-2
        self.inplace = old_detect.inplace
        self.stride = old_detect.stride
        self.use_dfl = old_detect.use_dfl
        # print(old_detect.use_dfl)
        self.reg_max = old_detect.reg_max
        self.proj_conv = old_detect.proj_conv
        self.grid_cell_offset = 0.5
        self.grid_cell_size = 5.0

        # Init decouple head
        self.stems = old_detect.stems
        self.cls_convs = old_detect.cls_convs
        self.reg_convs = old_detect.reg_convs
        if hasattr(old_detect, "cls_preds"):
            self.cls_preds = old_detect.cls_preds
        elif hasattr(old_detect, "cls_preds_af"):
            self.cls_preds = old_detect.cls_preds_af
        if hasattr(old_detect, "reg_preds"):
            self.reg_preds = old_detect.reg_preds
        elif hasattr(old_detect, "reg_preds_af"):
            self.reg_preds = old_detect.reg_preds_af

        self.use_rvc2 = use_rvc2

    def forward(self, x):
        outputs = []

        for i in range(self.nl):
            b, _, h, w = x[i].shape
            l = h * w
            x[i] = self.stems[i](x[i])
            cls_x = x[i]
            reg_x = x[i]
            cls_feat = self.cls_convs[i](cls_x)
            cls_output = self.cls_preds[i](cls_feat)
            reg_feat = self.reg_convs[i](reg_x)
            reg_output = self.reg_preds[i](reg_feat)

            if self.use_dfl:
                reg_output = reg_output.reshape([-1, 4, self.reg_max + 1, l]).permute(
                    0, 2, 1, 3
                )
                reg_output = self.proj_conv(F.softmax(reg_output, dim=1)).reshape(
                    [-1, 4, h, w]
                )

            cls_output = torch.sigmoid(cls_output)

            if self.use_rvc2:
                conf, _ = cls_output.max(1, keepdim=True)
            else:
                conf = torch.ones(
                    (cls_output.shape[0], 1, cls_output.shape[2], cls_output.shape[3]),
                    device=cls_output.device,
                )
            output = torch.cat([reg_output, conf, cls_output], axis=1)
            outputs.append(output)

        return outputs


class DetectV8(nn.Module):
    """YOLOv8 Detect head for detection models."""

    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, old_detect, use_rvc2: bool):
        super().__init__()
        self.nc = old_detect.nc  # number of classes
        self.nl = old_detect.nl  # number of detection layers
        self.reg_max = (
            old_detect.reg_max
        )  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = old_detect.no  # number of outputs per anchor
        self.stride = old_detect.stride  # strides computed during build

        self.cv2 = old_detect.cv2
        self.cv3 = old_detect.cv3
        self.dfl = old_detect.dfl
        self.f = old_detect.f
        self.i = old_detect.i

        self.use_rvc2 = use_rvc2

    def forward(self, x):
        shape = x[0].shape  # BCHW

        for i in range(self.nl):
            x[i] = torch.cat((self.cv2[i](x[i]), self.cv3[i](x[i])), 1)

        box, cls = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2).split(
            (self.reg_max * 4, self.nc), 1
        )
        box = self.dfl(box)
        cls_output = cls.sigmoid()
        # Get the max
        if self.use_rvc2:
            conf, _ = cls_output.max(1, keepdim=True)
        else:
            conf = torch.ones(
                (cls_output.shape[0], 1, cls_output.shape[2]), device=cls_output.device
            )
        # Concatenate
        y = torch.cat([box, conf, cls_output], axis=1)
        # Split to 3 channels
        outputs = []
        start, end = 0, 0
        for i, xi in enumerate(x):
            end += xi.shape[-2] * xi.shape[-1]
            outputs.append(
                y[:, :, start:end].view(xi.shape[0], -1, xi.shape[-2], xi.shape[-1])
            )
            start += xi.shape[-2] * xi.shape[-1]

        return outputs


class OBBV8(nn.Module):
    """YOLOv8 OBB detection head for detection with rotation models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, old_obb, use_rvc2):
        super().__init__()
        self.nc = old_obb.nc  # number of classes
        self.nl = old_obb.nl  # number of detection layers
        self.reg_max = old_obb.reg_max  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = old_obb.no  # number of outputs per anchor
        self.stride = old_obb.stride  # strides computed during build

        self.cv2 = old_obb.cv2
        self.cv3 = old_obb.cv3
        self.dfl = old_obb.dfl
        self.f = old_obb.f
        self.i = old_obb.i

        self.use_rvc2 = use_rvc2

        self.ne = old_obb.ne  # number of extra parameters
        self.cv4 = old_obb.cv4

    def forward(self, x):
        bs = x[0].shape[0]  # batch size
        angle = torch.cat([self.cv4[i](x[i]).view(bs, self.ne, -1) for i in range(self.nl)], 2)  # OBB theta logits
        # NOTE: set `angle` as an attribute so that `decode_bboxes` could use it.
        angle = (angle.sigmoid() - 0.25) * math.pi  # [-pi/4, 3pi/4]
        # ---------------------
        # Detection part
        # ---------------------
        for i in range(self.nl):
            x[i] = torch.cat((self.cv2[i](x[i]), self.cv3[i](x[i])), 1)

        box, cls = torch.cat([xi.view(bs, self.no, -1) for xi in x], 2).split((self.reg_max * 4, self.nc), 1)
        box = self.dfl(box)
        cls_output = cls.sigmoid()
        # Get the max
        if self.use_rvc2:
            conf, _ = cls_output.max(1, keepdim=True)
        else:
            conf = torch.ones((cls_output.shape[0], 1, cls_output.shape[2]), device=cls_output.device)
        # Concatenate
        y = torch.cat([box, conf, cls_output], axis=1)
        # Split to 3 channels
        outputs = []
        start, end = 0, 0
        for i, xi in enumerate(x):
          end += xi.shape[-2]*xi.shape[-1]
          outputs.append(y[:, :, start:end].view(xi.shape[0], -1, xi.shape[-2], xi.shape[-1]))
          start += xi.shape[-2]*xi.shape[-1]

        # Append the angle
        outputs.append(angle)
        
        return outputs


class PoseV8(nn.Module):
    """YOLOv8 Pose head for keypoints models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, old_kpts, use_rvc2):
        super().__init__()
        self.nc = old_kpts.nc  # number of classes
        self.nl = old_kpts.nl  # number of detection layers
        self.reg_max = old_kpts.reg_max  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = old_kpts.no  # number of outputs per anchor
        self.stride = old_kpts.stride  # strides computed during build

        self.cv2 = old_kpts.cv2
        self.cv3 = old_kpts.cv3
        self.dfl = old_kpts.dfl
        self.f = old_kpts.f
        self.i = old_kpts.i

        self.kpt_shape = old_kpts.kpt_shape # number of keypoints, number of dims (2 for x,y or 3 for x,y,visible)
        self.nk = old_kpts.nk  # number of keypoints total

        self.cv4 = old_kpts.cv4
        self.use_rvc2 = use_rvc2

    def forward(self, x):
        """Perform forward pass through YOLO model and return predictions."""
        bs = x[0].shape[0]  # batch size

        kpt = torch.cat([self.cv4[i](x[i]).view(bs, self.nk, -1) for i in range(self.nl)], -1)  # (bs, 17*3, h*w)
        # ---------------------
        # Detection part
        # ---------------------
        for i in range(self.nl):
            x[i] = torch.cat((self.cv2[i](x[i]), self.cv3[i](x[i])), 1)

        # box, cls = torch.cat([xi.view(bs, self.no, -1) for xi in x], 2).split((self.reg_max * 4, self.nc), 1)
        x_cat = torch.cat([xi.view(bs, self.no, -1) for xi in x], 2)
        if self.shape != bs:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = bs
        box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)

        box = self.dfl(box)
        cls_output = cls.sigmoid()
        # Get the max
        if self.use_rvc2:
            conf, _ = cls_output.max(1, keepdim=True)
        else:
            conf = torch.ones((cls_output.shape[0], 1, cls_output.shape[2]), device=cls_output.device)
        # Concatenate
        y = torch.cat([box, conf, cls_output], axis=1)
        # Split to 3 channels
        outputs = []
        start, end = 0, 0
        for i, xi in enumerate(x):
          end += xi.shape[-2]*xi.shape[-1]
          outputs.append(y[:, :, start:end].view(xi.shape[0], -1, xi.shape[-2], xi.shape[-1]))
          start += xi.shape[-2]*xi.shape[-1]

        pred_kpt = self.kpts_decode(bs, kpt)
        outputs.append(pred_kpt)

        return outputs

    def kpts_decode(self, bs, kpts):
        """Decodes keypoints."""
        ndim = self.kpt_shape[1]
        y = kpts.view(bs, *self.kpt_shape, -1)
        a = (y[:, :, :2] * 2.0 + (self.anchors - 0.5)) * self.strides
        if ndim == 3:
            a = torch.cat((a, y[:, :, 2:3].sigmoid()), 2)
        return a.view(bs, self.nk, -1)


class SegmentV8(nn.Module):
    """YOLOv8 Segment head for segmentation models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, old_segment, use_rvc2):
        super().__init__()
        self.nc = old_segment.nc  # number of classes
        self.nl = old_segment.nl  # number of detection layers
        self.reg_max = old_segment.reg_max  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = old_segment.no  # number of outputs per anchor
        self.stride = old_segment.stride  # strides computed during build

        self.cv2 = old_segment.cv2
        self.cv3 = old_segment.cv3
        self.dfl = old_segment.dfl
        self.f = old_segment.f
        self.i = old_segment.i

        self.nm = old_segment.nm  # number of masks
        self.npr = old_segment.npr  # number of protos
        self.proto = old_segment.proto  # protos
        self.detect = old_segment.detect

        self.cv4 = old_segment.cv4

        self.use_rvc2 = use_rvc2

    def forward(self, x):
        p = self.proto(x[0])  # mask protos
        bs = p.shape[0]  # batch size

        mc = [self.cv4[i](x[i]).view(bs, self.nm, -1) for i in range(self.nl)]  # mask coefficients
        # ---------------------
        # Detection part
        # ---------------------
        for i in range(self.nl):
            x[i] = torch.cat((self.cv2[i](x[i]), self.cv3[i](x[i])), 1)

        box, cls = torch.cat([xi.view(bs, self.no, -1) for xi in x], 2).split((self.reg_max * 4, self.nc), 1)
        box = self.dfl(box)
        cls_output = cls.sigmoid()
        # Get the max
        if self.use_rvc2:
            conf, _ = cls_output.max(1, keepdim=True)
        else:
            conf = torch.ones((cls_output.shape[0], 1, cls_output.shape[2]), device=cls_output.device)
        # Concatenate
        y = torch.cat([box, conf, cls_output], axis=1)
        # Split to 3 channels
        outputs = []
        start, end = 0, 0
        for i, xi in enumerate(x):
          end += xi.shape[-2]*xi.shape[-1]
          outputs.append(mc[i].view(xi.shape[0], -1, xi.shape[-2], xi.shape[-1]))
          outputs.append(y[:, :, start:end].view(xi.shape[0], -1, xi.shape[-2], xi.shape[-1]))
          start += xi.shape[-2]*xi.shape[-1]

        outputs.append(p)

        return outputs


class ClassifyV8(nn.Module):
    """YOLOv8 classification head, i.e. x(b,c1,20,20) to x(b,c2)."""
    def __init__(self, old_classify, use_rvc2: bool):
        super().__init__()
        self.conv = old_classify.conv
        self.pool = old_classify.pool
        self.drop = old_classify.drop
        self.linear = old_classify.linear
        self.f = old_classify.f
        self.i = old_classify.i
        
        self.use_rvc2 = use_rvc2

    def forward(self, x):
        """Performs a forward pass of the YOLO model on input image data."""
        if isinstance(x, list):
            x = torch.cat(x, 1)
        x = self.linear(self.drop(self.pool(self.conv(x)).flatten(1)))
        return x
