# Copyright (c) Alibaba, Inc. and its affiliates.
import torch
import torch.nn as nn
import sys
import os


def get_conv2d(in_channels, out_channels, kernel_size, stride, padding, dilation, groups, bias):
    if type(kernel_size) is int:
        use_large_impl = kernel_size > 5
    else:
        assert len(kernel_size) == 2 and kernel_size[0] == kernel_size[1]
        use_large_impl = kernel_size[0] > 5
    has_large_impl = 'LARGE_KERNEL_CONV_IMPL' in os.environ
    use_large_impl = False  # False is faster when the batch-size is small and resolution is large
    if has_large_impl and use_large_impl and in_channels == out_channels and out_channels == groups and stride == 1 and padding == kernel_size // 2 and dilation == 1:
        sys.path.append(os.environ['LARGE_KERNEL_CONV_IMPL'])
        #   Please follow the instructions https://github.com/DingXiaoH/RepLKNet-pytorch/blob/main/README.md
        #   export LARGE_KERNEL_CONV_IMPL=absolute_path_to_where_you_cloned_the_example (i.e., depthwise_conv2d_implicit_gemm.py)
        # TODO more efficient PyTorch implementations of large-kernel convolutions. Pull requests are welcomed.
        # Or you may try MegEngine. We have integrated an efficient implementation into MegEngine and it will automatically use it.
        from depthwise_conv2d_implicit_gemm import DepthWiseConv2dImplicitGEMM
        return DepthWiseConv2dImplicitGEMM(in_channels, kernel_size, bias=bias)
    else:
        return nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size, stride=stride,
                         padding=padding, dilation=dilation, groups=groups, bias=bias)


def conv_bn(in_channels, out_channels, kernel_size, stride, padding, groups, dilation=1):
    if padding is None:
        padding = kernel_size // 2
    result = nn.Sequential()
    result.add_module('conv', get_conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size,
                                         stride=stride, padding=padding, dilation=dilation, groups=groups, bias=False))
    result.add_module('bn', nn.BatchNorm2d(out_channels))
    return result


def fuse_bn(conv, bn):
    kernel = conv.weight
    running_mean = bn.running_mean
    running_var = bn.running_var
    gamma = bn.weight
    beta = bn.bias
    eps = bn.eps
    std = (running_var + eps).sqrt()
    t = (gamma / std).reshape(-1, 1, 1, 1)
    return kernel * t, beta - running_mean * gamma / std


class RepLKConv(nn.Module):
    # Re-param Depthwise LargeKernel convolution
    def __init__(self, in_channels, out_channels, kernel_size, stride, act=True,
                 small_kernel=None, small_kernel_merged=False):
        super(RepLKConv, self).__init__()
        self.kernel_size = kernel_size
        self.small_kernel = small_kernel
        # We assume the conv does not change the feature map size, so padding = k//2. Otherwise, you may configure padding as you wish, and change the padding of small_conv accordingly.
        padding = kernel_size // 2
        groups = in_channels
        if small_kernel_merged:
            self.lkb_reparam = get_conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size,
                                          stride=stride, padding=padding, dilation=1, groups=groups, bias=True)
        else:
            assert in_channels == out_channels == groups
            self.lkb_origin = conv_bn(in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size,
                                      stride=stride, padding=padding, dilation=1, groups=groups)
            if small_kernel is not None:
                assert small_kernel <= kernel_size, 'The kernel size for re-param cannot be larger than the large kernel!'
                self.small_conv = conv_bn(in_channels=in_channels, out_channels=out_channels, kernel_size=small_kernel,
                                             stride=stride, padding=small_kernel//2, groups=groups, dilation=1)
        self.act = nn.SiLU() if act is True else (act if isinstance(act, nn.Module) else nn.Identity())

    def forward(self, inputs):
        if hasattr(self, 'lkb_reparam'):
            out = self.lkb_reparam(inputs)
        else:
            # print(inputs.device, next(self.lkb_origin.named_parameters())[1].device)
            out = self.lkb_origin(inputs)
            if hasattr(self, 'small_conv'):
                out += self.small_conv(inputs)
        return self.act(out)

    def get_equivalent_kernel_bias(self):
        eq_k, eq_b = fuse_bn(self.lkb_origin.conv, self.lkb_origin.bn)
        if hasattr(self, 'small_conv'):
            small_k, small_b = fuse_bn(self.small_conv.conv, self.small_conv.bn)
            eq_b += small_b
            #   add to the central part
            eq_k += nn.functional.pad(small_k, [(self.kernel_size - self.small_kernel) // 2] * 4)
        return eq_k, eq_b

    def merge_kernel(self):
        eq_k, eq_b = self.get_equivalent_kernel_bias()
        self.lkb_reparam = get_conv2d(in_channels=self.lkb_origin.conv.in_channels,
                                     out_channels=self.lkb_origin.conv.out_channels,
                                     kernel_size=self.lkb_origin.conv.kernel_size, stride=self.lkb_origin.conv.stride,
                                     padding=self.lkb_origin.conv.padding, dilation=self.lkb_origin.conv.dilation,
                                     groups=self.lkb_origin.conv.groups, bias=True)
        self.lkb_reparam.weight.data = eq_k
        self.lkb_reparam.bias.data = eq_b
        self.__delattr__('lkb_origin')
        if hasattr(self, 'small_conv'):
            self.__delattr__('small_conv')