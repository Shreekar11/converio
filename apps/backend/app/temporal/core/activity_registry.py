from typing import Callable, Dict


class ActivityRegistry:
    _activities: Dict[str, Callable] = {}

    @classmethod
    def register(cls, category: str, name: str = None):
        def decorator(fn):
            activity_name = name or fn.__name__
            cls._activities[f"{category}:{activity_name}"] = fn
            return fn
        return decorator

    @classmethod
    def get_all_activities(cls) -> Dict[str, Callable]:
        return cls._activities
