from collections.abc import Callable


class ActivityRegistry:
    _activities: dict[str, Callable] = {}

    @classmethod
    def register(cls, category: str, name: str = None):
        def decorator(fn):
            activity_name = name or fn.__name__
            cls._activities[f"{category}:{activity_name}"] = fn
            return fn
        return decorator

    @classmethod
    def get_all_activities(cls) -> dict[str, Callable]:
        return cls._activities
