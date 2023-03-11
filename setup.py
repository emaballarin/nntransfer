from setuptools import setup

setup(
    name="nntransfer",
    version="0.1dev",
    description="Framework for transfer experiments",
    author="Arne Nix",
    author_email="arnenix@googlemail.com",
    packages=["nntransfer"],
    install_requires=['datajoint', 'matplotlib', 'neuralpredictors', 'nnfabrik', 'numpy', 'pandas', 'seaborn', 'torch', 'torchvision', 'tqdm'],  # external packages as dependencies
)
