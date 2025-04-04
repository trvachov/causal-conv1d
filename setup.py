# Copyright (c) 2025, Tri Dao.

import sys
import functools
import warnings
import os
import re
import ast
import glob
import shutil
from pathlib import Path
from packaging.version import parse, Version
import platform

from setuptools import setup, find_packages
import subprocess

import urllib.request
import urllib.error
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel

import torch
from torch.utils.cpp_extension import (
    BuildExtension,
    CppExtension,
    CUDAExtension,
    CUDA_HOME,
    ROCM_HOME,
    IS_HIP_EXTENSION,
)


with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()


# ninja build does not work unless include_dirs are abs path
this_dir = os.path.dirname(os.path.abspath(__file__))

BUILD_TARGET = os.environ.get("BUILD_TARGET", "auto")

if BUILD_TARGET == "auto":
    if IS_HIP_EXTENSION:
        IS_ROCM = True
    else:
        IS_ROCM = False
else:
    if BUILD_TARGET == "cuda":
        IS_ROCM = False
    elif BUILD_TARGET == "rocm":
        IS_ROCM = True

PACKAGE_NAME = "causal_conv1d"

BASE_WHEEL_URL = (
    "https://github.com/Dao-AILab/causal-conv1d/releases/download/{tag_name}/{wheel_name}"
)

# FORCE_BUILD: Force a fresh build locally, instead of attempting to find prebuilt wheels
# SKIP_CUDA_BUILD: Intended to allow CI to use a simple `python setup.py sdist` run to copy over raw files, without any cuda compilation
FORCE_BUILD = os.getenv("CAUSAL_CONV1D_FORCE_BUILD", "FALSE") == "TRUE"
SKIP_CUDA_BUILD = os.getenv("CAUSAL_CONV1D_SKIP_CUDA_BUILD", "FALSE") == "TRUE"
# For CI, we want the option to build with C++11 ABI since the nvcr images use C++11 ABI
FORCE_CXX11_ABI = os.getenv("CAUSAL_CONV1D_FORCE_CXX11_ABI", "FALSE") == "TRUE"
USE_TRITON_ROCM = os.getenv("CAUSAL_CONV1D_TRITON_AMD_ENABLE", "FALSE") == "TRUE"


@functools.lru_cache(maxsize=None)
def cuda_archs() -> str:
    return os.getenv("CAUSAL_CONV1D_CUDA_ARCHS", "80;90;100;120").split(";")

def get_arch():
    """
    Returns the system aarch for the current system.
    """
    if sys.platform.startswith("linux"):
        if platform.machine() == "x86_64":
            return "x86_64"
        elif platform.machine() == "arm64" or platform.machine() == "aarch64":
            return "aarch64"
        else:
            raise ValueError("Unsupported platform: {}".format(sys.platform))
    elif sys.platform == "darwin":
        mac_version = ".".join(platform.mac_ver()[0].split(".")[:2])
        return f"macosx_{mac_version}_x86_64"
    elif sys.platform == "win32":
        return "win_amd64"
    else:
        raise ValueError("Unsupported platform: {}".format(sys.platform))

def get_system() -> str:
    """
    Returns the system name as used in wheel filenames.
    """
    if platform.system() == "Windows":
        return "win"
    elif platform.system() == "Darwin":
        mac_version = ".".join(platform.mac_ver()[0].split(".")[:1])
        return f"macos_{mac_version}"
    elif platform.system() == "Linux":
        return "linux"
    else:
        raise ValueError("Unsupported system: {}".format(platform.system()))

def get_platform() -> str:
    """
    Returns the platform name as used in wheel filenames.
    """
    return f"{get_system()}_{get_arch()}"

def get_cuda_bare_metal_version(cuda_dir):
    raw_output = subprocess.check_output([cuda_dir + "/bin/nvcc", "-V"], universal_newlines=True)
    output = raw_output.split()
    release_idx = output.index("release") + 1
    bare_metal_version = parse(output[release_idx].split(",")[0])

    return raw_output, bare_metal_version


def get_hip_version():
    return parse(torch.version.hip.split()[-1].rstrip('-').replace('-', '+'))


def check_if_cuda_home_none(global_option: str) -> None:
    if CUDA_HOME is not None:
        return
    # warn instead of error because user could be downloading prebuilt wheels, so nvcc won't be necessary
    # in that case.
    warnings.warn(
        f"{global_option} was requested, but nvcc was not found.  Are you sure your environment has nvcc available?  "
        "If you're installing within a container from https://hub.docker.com/r/pytorch/pytorch, "
        "only images whose names contain 'devel' will provide nvcc."
    )


def check_if_rocm_home_none(global_option: str) -> None:
    if ROCM_HOME is not None:
        return
    # warn instead of error because user could be downloading prebuilt wheels, so hipcc won't be necessary
    # in that case.
    warnings.warn(
        f"{global_option} was requested, but hipcc was not found."
    )


def append_nvcc_threads(nvcc_extra_args):
    nvcc_threads = os.getenv("NVCC_THREADS") or "2"
    return nvcc_extra_args + ["--threads", nvcc_threads]


def rename_cpp_to_cu(cpp_files):
    for entry in cpp_files:
        shutil.copy(entry, os.path.splitext(entry)[0] + ".cu")


def validate_and_update_archs(archs):
    # List of allowed architectures
    allowed_archs = ["native", "gfx90a", "gfx940", "gfx941", "gfx942"]

    # Validate if each element in archs is in allowed_archs
    assert all(
        arch in allowed_archs for arch in archs
    ), f"One of GPU archs of {archs} is invalid or not supported by Causal-conv1d"


cmdclass = {}
ext_modules = []

if not SKIP_CUDA_BUILD and not IS_ROCM:
    print("\n\ntorch.__version__  = {}\n\n".format(torch.__version__))
    TORCH_MAJOR = int(torch.__version__.split(".")[0])
    TORCH_MINOR = int(torch.__version__.split(".")[1])

    check_if_cuda_home_none(PACKAGE_NAME)
    # Check, if CUDA11 is installed for compute capability 8.0
    cc_flag = []
    if CUDA_HOME is not None:
        _, bare_metal_version = get_cuda_bare_metal_version(CUDA_HOME)
        if bare_metal_version < Version("11.7"):
            raise RuntimeError(
                    f"{PACKAGE_NAME} is only supported on CUDA 11.6 and above.  "
                    "Note: make sure nvcc has a supported version by running nvcc -V."
            )

    if "80" in cuda_archs():
        cc_flag.append("-gencode")
        cc_flag.append("arch=compute_80,code=sm_80")
    if CUDA_HOME is not None:
        if bare_metal_version >= Version("11.8") and "90" in cuda_archs():
            cc_flag.append("-gencode")
            cc_flag.append("arch=compute_90,code=sm_90")
        if bare_metal_version >= Version("12.8") and "100" in cuda_archs():
            cc_flag.append("-gencode")
            cc_flag.append("arch=compute_100,code=sm_100")
        if bare_metal_version >= Version("12.8") and "120" in cuda_archs():
            cc_flag.append("-gencode")
            cc_flag.append("arch=compute_120,code=sm_120")

    # HACK: The compiler flag -D_GLIBCXX_USE_CXX11_ABI is set to be the same as
    # torch._C._GLIBCXX_USE_CXX11_ABI
    # https://github.com/pytorch/pytorch/blob/8472c24e3b5b60150096486616d98b7bea01500b/torch/utils/cpp_extension.py#L920
    if FORCE_CXX11_ABI:
        torch._C._GLIBCXX_USE_CXX11_ABI = True
        
    ext_modules.append(
        CUDAExtension(
            name="causal_conv1d_cuda",
            sources=[
                "csrc/causal_conv1d.cpp",
                "csrc/causal_conv1d_fwd.cu",
                "csrc/causal_conv1d_bwd.cu",
                "csrc/causal_conv1d_update.cu",
            ],
            extra_compile_args = {
            "cxx": ["-O3"],
            "nvcc": append_nvcc_threads(
                [
                    "-O3",
                    "-U__CUDA_NO_HALF_OPERATORS__",
                    "-U__CUDA_NO_HALF_CONVERSIONS__",
                    "-U__CUDA_NO_BFLOAT16_OPERATORS__",
                    "-U__CUDA_NO_BFLOAT16_CONVERSIONS__",
                    "-U__CUDA_NO_BFLOAT162_OPERATORS__",
                    "-U__CUDA_NO_BFLOAT162_CONVERSIONS__",
                    "--expt-relaxed-constexpr",
                    "--expt-extended-lambda",
                    "--use_fast_math",
                    "--ptxas-options=-v",
                    "-lineinfo",
                ]
                + cc_flag
            ),
        },
            include_dirs=[Path(this_dir) / "csrc" / "causal_conv1d"],
        )
    )
    
elif not SKIP_CUDA_BUILD and IS_ROCM:
    print("\n\ntorch.__version__  = {}\n\n".format(torch.__version__))
    TORCH_MAJOR = int(torch.__version__.split(".")[0])
    TORCH_MINOR = int(torch.__version__.split(".")[1])
    generator_flag = []
    torch_dir = torch.__path__[0]
    archs = os.getenv("GPU_ARCHS", "native").split(";")
    validate_and_update_archs(archs)
    cc_flag = [f"--offload-arch={arch}" for arch in archs]
    hip_version = get_hip_version()
    if hip_version > Version('5.7.23302'):
        cc_flag += ["-fno-offload-uniform-block"]
    if hip_version > Version('6.1.40090'):
        cc_flag += ["-mllvm", "-enable-post-misched=0"]
    if hip_version > Version('6.2.41132'):
        cc_flag += ["-mllvm", "-amdgpu-early-inline-all=true",
                    "-mllvm", "-amdgpu-function-calls=false"]
    if hip_version > Version('6.2.41133') and hip_version < Version('6.3.00000'):
        cc_flag += ["-mllvm", "-amdgpu-coerce-illegal-types=1"]
    if USE_TRITON_ROCM:
        # Skip C++ extension compilation if using Triton Backend
        pass
    else:
        extra_compile_args = {
            "cxx": ["-O3", "-std=c++17"],
            "nvcc": [
                "-O3",
                "-std=c++17",
                "-U__CUDA_NO_HALF_OPERATORS__",
                "-U__CUDA_NO_HALF_CONVERSIONS__",
                "-fgpu-flush-denormals-to-zero",
            ]
            + cc_flag,
        }


def get_package_version() -> str:
    import flash_attn
    flash_attn_init_file = flash_attn.__file__
    with open(flash_attn_init_file, "r") as f:
        version_match = re.search(r"^__version__\s*=\s*(.*)$", f.read(), re.MULTILINE)
    public_version = ast.literal_eval(version_match.group(1))
    local_version = os.environ.get("FLASH_ATTN_LOCAL_VERSION")
    if local_version:
        return f"{public_version}+{local_version}"
    else:
        return str(public_version)


def get_wheel_url() -> tuple[str, str]:
    torch_version_raw = parse(torch.__version__)
    python_version = f"cp{sys.version_info.major}{sys.version_info.minor}"
    platform_name = get_platform()
    causal_conv1d_version = get_package_version()
    torch_version = f"{torch_version_raw.major}.{torch_version_raw.minor}"
    cxx11_abi = str(torch._C._GLIBCXX_USE_CXX11_ABI).upper()

    if IS_ROCM:
        torch_hip_version = get_hip_version()
        hip_version = f"{torch_hip_version.major}{torch_hip_version.minor}"
        wheel_filename = f"{PACKAGE_NAME}-{causal_conv1d_version}+rocm{hip_version}torch{torch_version}cxx11abi{cxx11_abi}-{python_version}-{python_version}-{platform_name}.whl"
    else:
        # Determine the version numbers that will be used to determine the correct wheel
        # We're using the CUDA version used to build torch, not the one currently installed
        # _, cuda_version_raw = get_cuda_bare_metal_version(CUDA_HOME)
        torch_cuda_version = parse(torch.version.cuda)
        # For CUDA 11, we only compile for CUDA 11.8, and for CUDA 12 we only compile for CUDA 12.3
        # to save CI time. Minor versions should be compatible.
        torch_cuda_version = parse("11.8") if torch_cuda_version.major == 11 else parse("12.3")
        # cuda_version = f"{cuda_version_raw.major}{cuda_version_raw.minor}"
        cuda_version = f"{torch_cuda_version.major}"

        # Determine wheel URL based on CUDA version, torch version, python version and OS
        wheel_filename = f"{PACKAGE_NAME}-{causal_conv1d_version}+cu{cuda_version}torch{torch_version}cxx11abi{cxx11_abi}-{python_version}-{python_version}-{platform_name}.whl"

    wheel_url = BASE_WHEEL_URL.format(tag_name=f"v{causal_conv1d_version}", wheel_name=wheel_filename)

    return wheel_url, wheel_filename


class CachedWheelsCommand(_bdist_wheel):
    """
    The CachedWheelsCommand plugs into the default bdist wheel, which is ran by pip when it cannot
    find an existing wheel (which is currently the case for all cusual conv1d installs). We use
    the environment parameters to detect whether there is already a pre-built version of a compatible
    wheel available and short-circuits the standard full build pipeline.
    """

    def run(self) -> None:
        if FORCE_BUILD:
            return super().run()

        wheel_url, wheel_filename = get_wheel_url()
        print("Guessing wheel URL: ", wheel_url)
        try:
            urllib.request.urlretrieve(wheel_url, wheel_filename)

            # Make the archive
            # Lifted from the root wheel processing command
            # https://github.com/pypa/wheel/blob/cf71108ff9f6ffc36978069acb28824b44ae028e/src/wheel/bdist_wheel.py#LL381C9-L381C85
            if not os.path.exists(self.dist_dir):
                os.makedirs(self.dist_dir)

            impl_tag, abi_tag, plat_tag = self.get_tag()
            archive_basename = f"{self.wheel_dist_name}-{impl_tag}-{abi_tag}-{plat_tag}"

            wheel_path = os.path.join(self.dist_dir, archive_basename + ".whl")
            print("Raw wheel path", wheel_path)
            os.rename(wheel_filename, wheel_path)
        except (urllib.error.HTTPError, urllib.error.URLError):
            print("Precompiled wheel not found. Building from source...")
            # If the wheel could not be downloaded, build from source
            super().run()


class NinjaBuildExtension(BuildExtension):
    def __init__(self, *args, **kwargs) -> None:
        # do not override env MAX_JOBS if already exists
        if not os.environ.get("MAX_JOBS"):
            import psutil

            # calculate the maximum allowed NUM_JOBS based on cores
            max_num_jobs_cores = max(1, os.cpu_count() // 2)

            # calculate the maximum allowed NUM_JOBS based on free memory
            free_memory_gb = psutil.virtual_memory().available / (1024 ** 3)  # free memory in GB
            max_num_jobs_memory = int(free_memory_gb / 9)  # each JOB peak memory cost is ~8-9GB when threads = 4

            # pick lower value of jobs based on cores vs memory metric to minimize oom and swap usage during compilation
            max_jobs = max(1, min(max_num_jobs_cores, max_num_jobs_memory))
            os.environ["MAX_JOBS"] = str(max_jobs)

        super().__init__(*args, **kwargs)


setup(
    name=PACKAGE_NAME,
    version=get_package_version(),
    packages=find_packages(
        exclude=(
            "build",
            "csrc",
            "include",
            "tests",
            "dist",
            "docs",
            "benchmarks",
            "causal_conv1d.egg-info",
        )
    ),
    author="Tri Dao",
    author_email="tri@tridao.me",
    description="Causal depthwise conv1d in CUDA, with a PyTorch interface",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Dao-AILab/causal-conv1d",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: BSD License",
        "Operating System :: Unix",
    ],
    ext_modules=ext_modules,
    cmdclass={"bdist_wheel": CachedWheelsCommand, "build_ext": BuildExtension}
    if ext_modules
    else {
        "bdist_wheel": CachedWheelsCommand,
    },
    python_requires=">=3.9",
    install_requires=[
        "torch",
        "packaging",
        "ninja",
    ],
)
