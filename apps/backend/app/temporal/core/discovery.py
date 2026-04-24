import importlib
import pkgutil


def discover_all():
    discover_package("app.temporal.shared")
    discover_package("app.temporal.product")


def discover_package(package_name: str):
    package = importlib.import_module(package_name)
    for _, mod_name, is_pkg in pkgutil.walk_packages(package.__path__, f"{package_name}."):
        importlib.import_module(mod_name)
