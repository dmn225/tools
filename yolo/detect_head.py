import torch
import torch.nn as nn
import torch.nn.functional as F
import math

import sys
sys.path.append("./yolo/YOLOv6") # R2")


class DetectV2(nn.Module):
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
        if hasattr(old_detect, 'anchors'):
            self.anchors = old_detect.anchors 
        self.grid = old_detect.grid # [torch.zeros(1)] * self.nl
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
        self.cls_preds = old_detect.cls_preds
        self.reg_preds = old_detect.reg_preds

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
                reg_output = reg_output.reshape([-1, 4, self.reg_max + 1, l]).permute(0, 2, 1, 3)
                reg_output = self.proj_conv(F.softmax(reg_output, dim=1))[:, 0]
                reg_output = reg_output.reshape([-1, 4, h, w])
            
            cls_output = torch.sigmoid(cls_output)
            conf, _ = cls_output.max(1, keepdim=True)
            output = torch.cat([reg_output, conf, cls_output], axis=1)
            outputs.append(output)

        return outputs


class DetectV1(nn.Module):
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