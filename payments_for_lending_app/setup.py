from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

setup(
    name="payments_for_lending",
    version="0.0.1",
    description="ACH Autopay integration for ERPNext Lending module",
    author="Your Company",
    author_email="hello@example.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
