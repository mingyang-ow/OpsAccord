"""A recording stand-in for Streamlit, so the console can be driven in tests.

The operator console is a Streamlit script. Importing it normally would execute
Streamlit rendering outside a Streamlit runtime, and there would be no way to
"click" anything. This stub records every call the console makes and lets a test
declare which buttons are pressed, so the console's own click handlers -- the
code that posts an approval, an advance, a takeover -- run for real against the
real API.
"""

from __future__ import annotations

from typing import Any


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> bool:
        return False


class RecordingStreamlit:
    """Records rendering calls and answers the inputs a test declares."""

    def __init__(self, pressed: set[str] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.pressed = pressed or set()
        self.errors: list[str] = []
        self.session_state: dict[str, Any] = {}
        self.stopped = False

    # --- introspection ----------------------------------------------------

    def calls_named(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for called, args, kwargs in self.calls if called == name]

    def rendered_frames(self) -> list[Any]:
        return [args[0] for (args, _kwargs) in self.calls_named("dataframe") if args]

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    # --- structure --------------------------------------------------------

    def columns(self, spec: Any, **kwargs: Any) -> list[RecordingStreamlit]:
        self._record("columns", spec, **kwargs)
        count = spec if isinstance(spec, int) else len(spec)
        return [self for _ in range(count)]

    def tabs(self, labels: Any, **kwargs: Any) -> list[RecordingStreamlit]:
        self._record("tabs", labels, **kwargs)
        return [self for _ in labels]

    def container(self, *args: Any, **kwargs: Any) -> _NullContext:
        self._record("container", *args, **kwargs)
        return _NullContext()

    def expander(self, *args: Any, **kwargs: Any) -> _NullContext:
        self._record("expander", *args, **kwargs)
        return _NullContext()

    def form(self, *args: Any, **kwargs: Any) -> _NullContext:
        self._record("form", *args, **kwargs)
        return _NullContext()

    @property
    def sidebar(self) -> _NullContext:
        return _NullContext()

    def __enter__(self) -> RecordingStreamlit:
        # st.tabs(...) hands back context managers; the recorder itself
        # stands in for each tab so the with-block body runs.
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    # --- inputs the test controls ----------------------------------------

    def button(self, label: str, **kwargs: Any) -> bool:
        self._record("button", label, **kwargs)
        return label in self.pressed

    def form_submit_button(self, label: str, **kwargs: Any) -> bool:
        self._record("form_submit_button", label, **kwargs)
        return label in self.pressed

    def text_input(self, label: str, value: Any = "", **kwargs: Any) -> str:
        self._record("text_input", label, **kwargs)
        return str(value or "")

    def selectbox(self, label: str, options: Any = (), **kwargs: Any) -> Any:
        self._record("selectbox", label, **kwargs)
        values = list(options)
        return values[0] if values else None

    def radio(self, label: str, options: Any = (), **kwargs: Any) -> Any:
        self._record("radio", label, **kwargs)
        values = list(options)
        return values[0] if values else None

    # --- output -----------------------------------------------------------

    def error(self, message: str, **kwargs: Any) -> None:
        self.errors.append(message)
        self._record("error", message, **kwargs)

    def stop(self) -> None:
        self.stopped = True
        self._record("stop")
        raise RuntimeError("streamlit stop")

    def rerun(self) -> None:
        self._record("rerun")

    def __getattr__(self, name: str) -> Any:
        def _call(*args: Any, **kwargs: Any) -> None:
            self._record(name, *args, **kwargs)

        return _call


def install(
    monkeypatch: Any, module: Any, *, pressed: set[str] | None = None
) -> RecordingStreamlit:
    """Replace ``streamlit`` inside the console module and return the recorder."""
    recorder = RecordingStreamlit(pressed)
    monkeypatch.setattr(module, "st", recorder)
    return recorder


__all__ = ["RecordingStreamlit", "install"]
