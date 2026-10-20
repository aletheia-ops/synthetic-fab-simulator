"""Explicit native actions; checkpoint data never supplies executable code."""
from dataclasses import dataclass, asdict
from .checkpoint_format import CheckpointError


@dataclass(frozen=True)
class ActionRef:
    kind: str
    owner_id: str
    payload: object


class BoundAction:
    def __init__(self, registry, ref):
        self.registry, self.ref = registry, ref

    def __call__(self, *args):
        return self.registry.handlers[self.ref.kind](self.ref.owner_id, self.ref.payload, *args)


class ActionRegistry:
    def __init__(self):
        self.handlers = {}
        self.services = {}
        self.register('service:complete', self._service_complete)

    def _service_complete(self, owner_id, request_id):
        handler = self.services[owner_id]
        return handler._complete(handler.requests[request_id])

    def register(self, kind, handler):
        if not isinstance(kind, str) or not kind or kind in self.handlers or not callable(handler):
            raise CheckpointError('invalid or duplicate action kind')
        self.handlers[kind] = handler

    def validate(self, ref: ActionRef) -> None:
        if (type(ref) is not ActionRef or ref.kind not in self.handlers
                or not isinstance(ref.owner_id, str)):
            raise CheckpointError('unsupported native action')

    def bind(self, ref: ActionRef):
        self.validate(ref)
        return BoundAction(self, ref)


def export_action(action):
    if not isinstance(action, BoundAction):
        raise CheckpointError('opaque callback cannot be checkpointed')
    action.registry.validate(action.ref)
    return asdict(action.ref)


def restore_action(state, registry):
    try: return registry.bind(ActionRef(**state))
    except (TypeError, ValueError) as error:
        raise CheckpointError('invalid action reference') from error
