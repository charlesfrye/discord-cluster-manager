from utils import verbose_allclose
import torch
import torch.nn.functional as F
from task import input_t, output_t, KernelSpec

def ref_kernel(data: input_t, spec: KernelSpec) -> output_t:
    """
    Reference implementation of 2D convolution using PyTorch.
    Args:
        data: Tuple of (input tensor, kernel tensor)
        spec: Convolution specifications (stride, padding)
    Returns:
        Output tensor after convolution
    """
    input_tensor, kernel = data
    return F.conv2d(
        input_tensor, 
        kernel,
        stride=spec.stride,
        padding=spec.padding
    )

def generate_input(size: int, kernel_size: int, channels: int, batch: int, seed: int) -> input_t:
    """
    Generates random input and kernel tensors.
    Returns:
        Tuple of (input tensor, kernel tensor)
    """
    gen = torch.Generator(device='cuda')
    gen.manual_seed(seed)
    
    # Generate input tensor: [batch, in_channels, height, width]
    input_tensor = torch.randn(
        batch, channels, size, size,
        device='cuda', 
        dtype=torch.float32, 
        generator=gen
    ).contiguous()
    
    # Generate kernel tensor: [out_channels, in_channels, kernel_height, kernel_width]
    # Here we use same number of output channels as input channels for simplicity
    kernel = torch.randn(
        channels, channels, kernel_size, kernel_size,
        device='cuda',
        dtype=torch.float32,
        generator=gen
    ).contiguous()
    
    return (input_tensor, kernel)

def check_implementation(
    data: input_t,
    spec: KernelSpec,
    output: output_t,
) -> str:
    expected = ref_kernel(data, spec)
    reasons = verbose_allclose(output, expected, rtol=1e-3, atol=1e-3)
    
    if len(reasons) > 0:
        return "mismatch found! custom implementation doesn't match reference: " + reasons[0]
    
    return '' 