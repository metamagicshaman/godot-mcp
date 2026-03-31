from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GODOT_EXECUTABLE_ENV = "GODOT_EXECUTABLE"
MINIMUM_GODOT_VERSION = (4, 5)
DOCS_API_FILENAME = "extension_api.json"
ACTIVE_PROCESSES: list[subprocess.Popen[Any]] = []
KNOWN_SHADER_TYPES = {"canvas_item", "spatial", "particles", "sky", "fog"}
DEBUG_ERROR_TOKENS = ("SCRIPT ERROR:", "USER ERROR:", "CRITICAL:", "ERROR:")
DEBUG_WARNING_TOKENS = ("SCRIPT WARNING:", "USER WARNING:", "WARNING:")
RESOURCE_CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "scripts": {".gd", ".cs"},
    "shaders": {".gdshader", ".gdshaderinc", ".shader"},
    "scenes": {".tscn", ".scn"},
    "textures": {".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp", ".tga", ".exr", ".hdr", ".dds", ".ktx"},
}


class GodotError(RuntimeError):
    """Raised when a Godot operation fails."""


@dataclass(slots=True)
class ProcessLaunchResult:
    pid: int
    command: list[str]
    log_path: str


def snake_case_name(value: str, default: str = "untitled") -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value)
    if not parts:
        return default
    return "_".join(part.lower() for part in parts)


def pascal_case_name(value: str, default: str = "Main") -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value)
    if not parts:
        return default
    return "".join(part[:1].upper() + part[1:] for part in parts)


def normalize_project_subdir(value: str) -> str:
    raw = value.replace("\\", "/").strip()
    if not raw or raw == ".":
        return ""
    if raw.startswith("/"):
        raise GodotError("Project-relative directories must not be absolute paths.")

    parts: list[str] = []
    for part in raw.split("/"):
        cleaned = part.strip()
        if not cleaned or cleaned == ".":
            continue
        if cleaned == "..":
            raise GodotError("Project-relative directories must not escape the project root.")
        parts.append(snake_case_name(cleaned))

    return "/".join(parts)


def normalize_scene_node_path(value: str | None) -> str:
    raw = (value or ".").replace("\\", "/").strip()
    if not raw or raw == ".":
        return "."
    if raw.startswith("/"):
        raise GodotError("Scene node paths must be relative to the scene root.")

    parts: list[str] = []
    for part in raw.split("/"):
        cleaned = part.strip()
        if not cleaned or cleaned == ".":
            continue
        if cleaned == "..":
            raise GodotError("Scene node paths must not escape the scene root.")
        parts.append(cleaned)

    return "/".join(parts) if parts else "."


def canonical_scene_node_path(value: str | None, empty_value: str = "") -> str:
    raw = (value or "").replace("\\", "/").strip()
    if not raw:
        return empty_value
    if raw == ".":
        return "."
    if raw.startswith("./"):
        raw = raw[2:]
    return normalize_scene_node_path(raw)


def parse_version_tuple(version_output: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", version_output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _candidate_macos_executables() -> list[Path]:
    roots = [Path("/Applications"), Path.home() / "Applications"]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for app_path in root.glob("*.app"):
            if "godot" not in app_path.name.lower():
                continue
            macos_dir = app_path / "Contents" / "MacOS"
            if not macos_dir.exists():
                continue
            for executable in macos_dir.iterdir():
                if executable.is_file():
                    candidates.append(executable)
    return candidates


def _normalize_executable_path(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser()
    if path.suffix.lower() == ".app":
        macos_dir = path / "Contents" / "MacOS"
        if not macos_dir.exists():
            raise GodotError(f"Could not find a runnable executable inside {path}.")
        executables = [entry for entry in macos_dir.iterdir() if entry.is_file()]
        if not executables:
            raise GodotError(f"Could not find a runnable executable inside {path}.")
        return executables[0]
    return path


def get_godot_version(executable: str | Path) -> str:
    path = str(_normalize_executable_path(executable))
    result = subprocess.run(
        [path, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise GodotError(f"Failed to query Godot version from {path}: {details or 'unknown error'}")

    version = (result.stdout or result.stderr).strip().splitlines()
    if not version:
        raise GodotError(f"Godot executable at {path} did not return a version string.")

    version_text = version[0].strip()
    parsed = parse_version_tuple(version_text)
    if parsed is not None and parsed < MINIMUM_GODOT_VERSION:
        required = ".".join(str(part) for part in MINIMUM_GODOT_VERSION)
        raise GodotError(f"Godot {required}+ is required, but {version_text} was found.")
    return version_text


def resolve_godot_executable(explicit: str | None = None) -> tuple[Path, str]:
    candidates: list[str | Path] = []
    if explicit:
        candidates.append(explicit)
    env_value = os.getenv(GODOT_EXECUTABLE_ENV)
    if env_value:
        candidates.append(env_value)

    for command_name in ("godot", "godot4", "godot-mono", "godot4-mono"):
        found = shutil.which(command_name)
        if found:
            candidates.append(found)

    if platform.system() == "Darwin":
        candidates.extend(_candidate_macos_executables())

    checked: list[str] = []
    for candidate in candidates:
        try:
            normalized = _normalize_executable_path(candidate)
            version = get_godot_version(normalized)
            return normalized.resolve(), version
        except (FileNotFoundError, GodotError, PermissionError):
            checked.append(str(candidate))

    extra = ""
    if checked:
        extra = f" Checked: {', '.join(checked)}."
    raise GodotError(
        "Could not find a usable Godot 4.5+ executable. "
        f"Pass `godot_executable` to the tool call or set {GODOT_EXECUTABLE_ENV}.{extra}"
    )


def ensure_project_path(project_path: str) -> Path:
    candidate = Path(project_path).expanduser()
    if candidate.name == "project.godot":
        candidate = candidate.parent
    project_dir = candidate.resolve()
    project_file = project_dir / "project.godot"
    if not project_file.exists():
        raise GodotError(f"No Godot project was found at {project_dir}. Expected {project_file}.")
    return project_dir


def resolve_scene_path(project_dir: Path, scene_path: str) -> tuple[Path, str]:
    if scene_path.startswith("res://"):
        relative = scene_path.removeprefix("res://")
        absolute = (project_dir / relative).resolve()
    else:
        absolute_candidate = Path(scene_path).expanduser()
        if absolute_candidate.is_absolute():
            absolute = absolute_candidate.resolve()
        else:
            absolute = (project_dir / absolute_candidate).resolve()

    try:
        relative_path = absolute.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise GodotError("Scene paths must stay inside the chosen Godot project.") from exc

    if absolute.suffix.lower() != ".tscn":
        raise GodotError("Scene paths must point to a `.tscn` file.")
    return absolute, f"res://{relative_path.as_posix()}"


def resolve_project_file_path(project_dir: Path, file_path: str) -> tuple[Path, str]:
    if file_path.startswith("res://"):
        relative = file_path.removeprefix("res://")
        absolute = (project_dir / relative).resolve()
    else:
        absolute_candidate = Path(file_path).expanduser()
        if absolute_candidate.is_absolute():
            absolute = absolute_candidate.resolve()
        else:
            absolute = (project_dir / absolute_candidate).resolve()

    try:
        relative_path = absolute.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise GodotError("Project file paths must stay inside the chosen Godot project.") from exc

    return absolute, f"res://{relative_path.as_posix()}"


def resolve_project_directory_path(project_dir: Path, folder_path: str | None = None) -> tuple[Path, str]:
    raw = (folder_path or "").strip()
    if not raw or raw in {".", "res://"}:
        return project_dir, "res://"

    absolute, resource_path = resolve_project_file_path(project_dir, raw)
    if not absolute.exists():
        raise GodotError(f"Folder not found: {absolute}")
    if not absolute.is_dir():
        raise GodotError(f"Path is not a folder: {absolute}")
    return absolute, resource_path


def _parse_script_json_output(output: str, script_name: str) -> dict[str, Any]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise GodotError(f"Godot helper script `{script_name}` did not return a JSON object.")


def _run_godot_script(
    executable: Path,
    project_dir: Path,
    script_name: str,
    user_args: list[str],
    timeout: int = 60,
) -> str:
    script_path = Path(__file__).resolve().parent / "templates" / script_name
    if not script_path.exists():
        raise GodotError(f"Missing helper script: {script_path}")

    log_path = _create_log_path(project_dir, f"helper-{Path(script_name).stem}")
    command = [
        str(executable),
        "--log-file",
        str(log_path),
        "--headless",
        "--path",
        str(project_dir),
        "-s",
        str(script_path),
        "--",
        *user_args,
    ]
    result = subprocess.run(
        command,
        cwd=project_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        details = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()
        raise GodotError(
            f"Godot helper script `{script_name}` failed.\n"
            f"Log file: {log_path}\n"
            f"{details or 'No output was returned.'}"
        )
    return result.stdout.strip()


def _run_godot_script_with_capture(
    executable: Path,
    project_dir: Path,
    script_name: str,
    user_args: list[str],
    timeout: int = 60,
) -> dict[str, Any]:
    script_path = Path(__file__).resolve().parent / "templates" / script_name
    if not script_path.exists():
        raise GodotError(f"Missing helper script: {script_path}")

    log_path = _create_log_path(project_dir, f"helper-{Path(script_name).stem}")
    command = [
        str(executable),
        "--log-file",
        str(log_path),
        "--headless",
        "--path",
        str(project_dir),
        "-s",
        str(script_path),
        "--",
        *user_args,
    ]
    result = subprocess.run(
        command,
        cwd=project_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "log_path": str(log_path),
        "log_output": log_text,
    }


def _create_log_path(cwd: Path, log_name: str) -> Path:
    logs_dir = cwd / ".godot-mcp" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return logs_dir / f"{timestamp}-{log_name}.log"


def _launch_process(command: list[str], cwd: Path, log_name: str) -> ProcessLaunchResult:
    log_path = _create_log_path(cwd, log_name)
    command_with_log = [command[0], "--log-file", str(log_path), *command[1:]]

    popen_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "start_new_session": platform.system() != "Windows",
    }
    if platform.system() == "Windows":
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200

    with open(log_path, "a", encoding="utf-8") as handle:
        process = subprocess.Popen(
            command_with_log,
            stdout=handle,
            stderr=handle,
            **popen_kwargs,
        )

    _prune_active_processes()
    if process.poll() is None:
        ACTIVE_PROCESSES.append(process)

    return ProcessLaunchResult(pid=process.pid, command=command_with_log, log_path=str(log_path))


def _prune_active_processes() -> None:
    ACTIVE_PROCESSES[:] = [process for process in ACTIVE_PROCESSES if process.poll() is None]


def _truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
    if max_chars < 1:
        raise GodotError("`max_output_chars` must be at least 1.")
    if len(value) <= max_chars:
        return value, False
    marker = "[truncated]\n"
    if max_chars <= len(marker):
        return value[-max_chars:], True
    return marker + value[-(max_chars - len(marker)) :], True


def _classify_debug_lines(stdout_text: str, stderr_text: str, log_text: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    seen: set[str] = set()

    def add_line(bucket: list[str], source: str, line: str) -> None:
        entry = f"[{source}] {line}"
        if entry not in seen:
            seen.add(entry)
            bucket.append(entry)

    for source, text in (("stderr", stderr_text), ("log", log_text), ("stdout", stdout_text)):
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            upper_line = line.upper()
            if any(token in upper_line for token in DEBUG_ERROR_TOKENS):
                add_line(errors, source, line)
            elif any(token in upper_line for token in DEBUG_WARNING_TOKENS):
                add_line(warnings, source, line)
            elif source in {"stderr", "log"}:
                add_line(info, source, line)

    return {
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "info_count": len(info),
    }


def _build_scene_tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    ordered_nodes: list[dict[str, Any]] = []
    for node in nodes:
        node_copy = dict(node)
        node_copy["children"] = []
        by_path[str(node_copy.get("path", ""))] = node_copy
        ordered_nodes.append(node_copy)

    root_node = by_path.get(".")
    roots: list[dict[str, Any]] = []
    for node in ordered_nodes:
        path = str(node.get("path", ""))
        parent_path = str(node.get("parent_path", ""))
        if path == ".":
            roots.append(node)
            continue
        if not parent_path and root_node is not None:
            root_node["children"].append(node)
            continue
        if not parent_path or parent_path == path or parent_path not in by_path:
            roots.append(node)
            continue
        by_path[parent_path]["children"].append(node)

    return roots


def _docs_cache_root() -> Path:
    return Path.home() / ".cache" / "godot-mcp" / "docs"


def _docs_cache_key(executable: Path, version: str) -> str:
    digest = hashlib.sha256(str(executable.resolve()).encode("utf-8")).hexdigest()[:12]
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "_", version)
    return f"{safe_version}-{digest}"


def _strip_doc_markup(value: str) -> str:
    cleaned = value.replace("$DOCS_URL", "https://docs.godotengine.org")
    cleaned = re.sub(r"\[/?codeblocks?\]", "", cleaned)
    cleaned = re.sub(r"\[/?gdscript\]", "", cleaned)
    cleaned = re.sub(r"\[/?csharp\]", "", cleaned)
    cleaned = re.sub(r"\[/?code\]", "`", cleaned)
    cleaned = re.sub(r"\[/?b\]", "", cleaned)
    cleaned = re.sub(r"\[/?i\]", "", cleaned)
    cleaned = re.sub(r"\[(param|member|method|signal|constant|enum|class|annotation) ([^\]]+)\]", r"\2", cleaned)
    cleaned = re.sub(r"\[url=([^\]]+)\]([^\[]+)\[/url\]", r"\2 (\1)", cleaned)
    cleaned = re.sub(r"\[/?[A-Za-z_]+(?:=[^\]]+)?\]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_search_text(value: str) -> str:
    return " ".join(_tokenize_search_text(value))


def _tokenize_search_text(value: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9]+", value.lower()) if token]


def _score_identifier(name: str, query_text: str, query_tokens: set[str]) -> int:
    if not query_text:
        return 0

    normalized_name = _normalize_search_text(name)
    score = 0
    if normalized_name == query_text:
        score += 140
    elif normalized_name.startswith(query_text):
        score += 100
    elif query_text in normalized_name:
        score += 65

    score += len(set(_tokenize_search_text(name)) & query_tokens) * 25
    return score


def _score_description(text: str, query_text: str, query_tokens: set[str]) -> int:
    if not query_text:
        return 0

    normalized_text = _normalize_search_text(text)
    score = 0
    if query_text in normalized_text:
        score += 20
    score += len(set(_tokenize_search_text(text)) & query_tokens) * 5
    return score


def _compact_doc_text(value: str, limit: int = 420) -> str:
    stripped = _strip_doc_markup(value)
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3].rstrip() + "..."


def _format_arguments(arguments: list[dict[str, Any]]) -> str:
    formatted: list[str] = []
    for argument in arguments:
        name = str(argument.get("name", "arg"))
        arg_type = str(argument.get("type", "Variant"))
        text = f"{name}: {arg_type}"
        if "default_value" in argument:
            text += f" = {argument['default_value']}"
        formatted.append(text)
    return ", ".join(formatted)


def _format_match_signature(kind: str, class_name: str, item: dict[str, Any]) -> str:
    if kind == "class":
        inherits = str(item.get("inherits", "")).strip()
        return f"class {class_name} extends {inherits}" if inherits else f"class {class_name}"
    if kind == "method":
        return f"{class_name}.{item['name']}({_format_arguments(item.get('arguments', []))})"
    if kind == "property":
        return f"{class_name}.{item['name']}: {item.get('type', 'Variant')}"
    if kind == "signal":
        return f"{class_name}.{item['name']}({_format_arguments(item.get('arguments', []))})"
    if kind == "constant":
        return f"{class_name}.{item['name']} = {item.get('value')}"
    return f"{class_name}.{item.get('name', '')}"


def _default_shader_source(shader_type: str) -> str:
    if shader_type == "canvas_item":
        return (
            "shader_type canvas_item;\n\n"
            "void fragment() {\n"
            "\t// COLOR = texture(TEXTURE, UV);\n"
            "}\n"
        )
    if shader_type == "spatial":
        return (
            "shader_type spatial;\n\n"
            "void fragment() {\n"
            "\t// ALBEDO = vec3(1.0);\n"
            "}\n"
        )
    if shader_type == "particles":
        return (
            "shader_type particles;\n\n"
            "void start() {\n"
            "\t// Called once when the particle spawns.\n"
            "}\n\n"
            "void process() {\n"
            "\t// Called each frame for each particle.\n"
            "}\n"
        )
    if shader_type == "sky":
        return (
            "shader_type sky;\n\n"
            "void sky() {\n"
            "\t// COLOR = vec3(0.0);\n"
            "}\n"
        )
    if shader_type == "fog":
        return (
            "shader_type fog;\n\n"
            "void fog() {\n"
            "\t// ALBEDO = vec3(1.0);\n"
            "}\n"
        )
    return f"shader_type {shader_type};\n\n"


def _default_script_source(base_class: str) -> str:
    final_base_class = base_class.strip() or "Node"
    return (
        f"extends {final_base_class}\n\n"
        "func _ready() -> void:\n"
        "\tpass\n"
    )


def _project_resource_path(project_dir: Path, target: Path) -> str:
    relative_path = target.relative_to(project_dir)
    relative_text = relative_path.as_posix()
    if not relative_text or relative_text == ".":
        return "res://"
    return f"res://{relative_text}"


def _collect_project_entries(
    project_dir: Path,
    current_dir: Path,
    max_depth: int,
    include_hidden: bool,
    depth: int = 0,
) -> tuple[list[dict[str, Any]], int, int]:
    entries: list[dict[str, Any]] = []
    directory_count = 0
    file_count = 0

    children = sorted(
        current_dir.iterdir(),
        key=lambda child: (not child.is_dir(), child.name.lower(), child.name),
    )
    for child in children:
        if not include_hidden and child.name.startswith("."):
            continue

        resource_path = _project_resource_path(project_dir, child)
        if child.is_dir():
            directory_count += 1
            entry: dict[str, Any] = {
                "name": child.name,
                "type": "directory",
                "path": str(child),
                "resource_path": resource_path,
                "children": [],
            }
            if depth < max_depth:
                nested_entries, nested_directory_count, nested_file_count = _collect_project_entries(
                    project_dir=project_dir,
                    current_dir=child,
                    max_depth=max_depth,
                    include_hidden=include_hidden,
                    depth=depth + 1,
                )
                entry["children"] = nested_entries
                directory_count += nested_directory_count
                file_count += nested_file_count
            else:
                entry["truncated"] = True

            entries.append(entry)
            continue

        entries.append(
            {
                "name": child.name,
                "type": "file",
                "path": str(child),
                "resource_path": resource_path,
                "size_bytes": child.stat().st_size,
            }
        )
        file_count += 1

    return entries, directory_count, file_count


def _render_project_tree(root_label: str, entries: list[dict[str, Any]]) -> str:
    lines = [root_label]

    def visit(children: list[dict[str, Any]], prefix: str) -> None:
        for index, child in enumerate(children):
            is_last = index == len(children) - 1
            connector = "└── " if is_last else "├── "
            suffix = "/" if child.get("type") == "directory" else ""
            lines.append(f"{prefix}{connector}{child['name']}{suffix}")

            child_prefix = prefix + ("    " if is_last else "│   ")
            nested_children = child.get("children", [])
            if child.get("type") == "directory" and isinstance(nested_children, list) and nested_children:
                visit(nested_children, child_prefix)
            elif child.get("type") == "directory" and child.get("truncated"):
                lines.append(f"{child_prefix}└── ...")

    visit(entries, "")
    return "\n".join(lines)


def _filter_scene_validation_errors(
    errors: list[str],
    scene_resource_path: str,
    keep_fallback: bool,
) -> list[str]:
    interesting_tokens = (
        scene_resource_path.lower(),
        "parse error",
        "failed loading resource",
        "error loading resource",
        "packedscene",
        "scene state",
        "could not parse the scene resource",
    )
    filtered = [
        line
        for line in errors
        if any(token in line.lower() for token in interesting_tokens)
    ]
    if filtered:
        return filtered
    return errors if keep_fallback else []


def _scan_project_resources(project_dir: Path, root_dir: Path) -> dict[str, list[dict[str, Any]]]:
    categorized: dict[str, list[dict[str, Any]]] = {name: [] for name in RESOURCE_CATEGORY_EXTENSIONS}

    for current_root, dir_names, file_names in os.walk(root_dir):
        if Path(current_root) != root_dir:
            dir_names[:] = [name for name in dir_names if not name.startswith(".")]
        else:
            dir_names[:] = [name for name in dir_names if not name.startswith(".")]

        for file_name in sorted(file_names):
            if file_name.startswith("."):
                continue

            absolute_path = (Path(current_root) / file_name).resolve()
            suffix = absolute_path.suffix.lower()
            category = next(
                (
                    name
                    for name, extensions in RESOURCE_CATEGORY_EXTENSIONS.items()
                    if suffix in extensions
                ),
                None,
            )
            if category is None:
                continue

            categorized[category].append(
                {
                    "name": absolute_path.name,
                    "path": str(absolute_path),
                    "resource_path": _project_resource_path(project_dir, absolute_path),
                    "extension": suffix,
                    "size_bytes": absolute_path.stat().st_size,
                }
            )

    for entries in categorized.values():
        entries.sort(key=lambda entry: (str(entry["resource_path"]).lower(), str(entry["resource_path"])))

    return categorized


_PROFILER_AUTOLOAD_KEY = "_GodotMcpProfiler"
_PROFILER_SCRIPT_NAME = "profiler.gd"

_CAMERA_CONTROLLER_AUTOLOAD_KEY = "_GodotMcpCameraController"
_CAMERA_CONTROLLER_SCRIPT_NAME = "camera_controller.gd"


def _inject_profiler_autoload(project_dir: Path) -> tuple[Path, str]:
    src = Path(__file__).resolve().parent / "templates" / _PROFILER_SCRIPT_NAME
    if not src.exists():
        raise GodotError(f"Missing profiler helper script: {src}")

    dest_dir = project_dir / ".godot-mcp"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _PROFILER_SCRIPT_NAME
    shutil.copy2(src, dest)

    project_file = project_dir / "project.godot"
    original_content = project_file.read_text(encoding="utf-8")

    autoload_line = f'{_PROFILER_AUTOLOAD_KEY}="*res://.godot-mcp/{_PROFILER_SCRIPT_NAME}"'

    if "[autoload]" in original_content:
        modified = original_content.replace(
            "[autoload]", f"[autoload]\n\n{autoload_line}", 1
        )
    else:
        modified = original_content.rstrip() + f"\n\n[autoload]\n\n{autoload_line}\n"

    project_file.write_text(modified, encoding="utf-8")
    return dest, original_content


def _remove_profiler_autoload(
    project_dir: Path, original_content: str, script_dest: Path
) -> None:
    project_file = project_dir / "project.godot"
    project_file.write_text(original_content, encoding="utf-8")
    script_dest.unlink(missing_ok=True)


def _inject_camera_controller_autoload(project_dir: Path) -> tuple[Path, str]:
    src = Path(__file__).resolve().parent / "templates" / _CAMERA_CONTROLLER_SCRIPT_NAME
    if not src.exists():
        raise GodotError(f"Missing camera controller helper script: {src}")

    dest_dir = project_dir / ".godot-mcp"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _CAMERA_CONTROLLER_SCRIPT_NAME
    shutil.copy2(src, dest)

    project_file = project_dir / "project.godot"
    original_content = project_file.read_text(encoding="utf-8")

    autoload_line = f'{_CAMERA_CONTROLLER_AUTOLOAD_KEY}="*res://.godot-mcp/{_CAMERA_CONTROLLER_SCRIPT_NAME}"'

    if "[autoload]" in original_content:
        modified = original_content.replace(
            "[autoload]", f"[autoload]\n\n{autoload_line}", 1
        )
    else:
        modified = original_content.rstrip() + f"\n\n[autoload]\n\n{autoload_line}\n"

    project_file.write_text(modified, encoding="utf-8")
    return dest, original_content


def _remove_camera_controller_autoload(
    project_dir: Path, original_content: str, script_dest: Path
) -> None:
    project_file = project_dir / "project.godot"
    project_file.write_text(original_content, encoding="utf-8")
    script_dest.unlink(missing_ok=True)


def _compute_aggregate_stats(
    samples: list[dict[str, Any]], keys: list[str]
) -> dict[str, dict[str, float]]:
    import math

    stats: dict[str, dict[str, float]] = {}
    for key in keys:
        values = [s[key] for s in samples if key in s and isinstance(s[key], (int, float))]
        if not values:
            continue
        values_sorted = sorted(values)
        n = len(values_sorted)
        avg = sum(values_sorted) / n
        stats[key] = {
            "min": values_sorted[0],
            "max": values_sorted[-1],
            "avg": round(avg, 4),
            "median": values_sorted[n // 2],
            "p95": values_sorted[min(math.ceil(n * 0.95) - 1, n - 1)],
            "p99": values_sorted[min(math.ceil(n * 0.99) - 1, n - 1)],
            "sample_count": n,
        }
    return stats


_PERFORMANCE_STAT_KEYS = [
    "fps",
    "frame_time_ms",
    "process_time_ms",
    "physics_time_ms",
    "physics_frame_time_ms",
    "navigation_process_ms",
    "memory_static_bytes",
    "object_count",
    "resource_count",
    "node_count",
    "orphan_node_count",
    "physics_2d_active_objects",
    "physics_2d_collision_pairs",
    "physics_2d_island_count",
    "physics_3d_active_objects",
    "physics_3d_collision_pairs",
    "physics_3d_island_count",
    "audio_output_latency_ms",
]

_VISUAL_STAT_KEYS = [
    "fps",
    "frame_time_ms",
    "render_objects_in_frame",
    "render_primitives_in_frame",
    "render_draw_calls_in_frame",
    "render_video_mem_bytes",
    "navigation_process_ms",
    "object_count",
    "node_count",
]


class GodotController:
    def __init__(self) -> None:
        self._docs_cache: dict[str, dict[str, Any]] = {}

    def _get_docs_api(
        self,
        executable: Path,
        version: str,
        refresh_cache: bool = False,
    ) -> tuple[Path, dict[str, Any]]:
        cache_dir = _docs_cache_root() / _docs_cache_key(executable, version)
        json_path = cache_dir / DOCS_API_FILENAME

        if refresh_cache and cache_dir.exists():
            shutil.rmtree(cache_dir)
            self._docs_cache.pop(str(json_path), None)

        if not json_path.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            log_path = _create_log_path(cache_dir, "dump-extension-api-with-docs")
            command = [
                str(executable),
                "--log-file",
                str(log_path),
                "--headless",
                "--dump-extension-api-with-docs",
                "--quit",
            ]
            result = subprocess.run(
                command,
                cwd=cache_dir,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode != 0 or not json_path.exists():
                details = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()
                raise GodotError(
                    "Failed to generate the local Godot docs cache.\n"
                    f"Log file: {log_path}\n"
                    f"{details or 'No output was returned.'}"
                )

        cache_key = str(json_path)
        if cache_key not in self._docs_cache:
            try:
                self._docs_cache[cache_key] = json.loads(json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise GodotError(f"Failed to parse cached Godot docs at {json_path}.") from exc

        return json_path, self._docs_cache[cache_key]

    def detect_executable(self, godot_executable: str | None = None) -> dict[str, Any]:
        executable, version = resolve_godot_executable(godot_executable)
        return {
            "executable": str(executable),
            "version": version,
            "minimum_supported_version": ".".join(str(value) for value in MINIMUM_GODOT_VERSION),
        }

    def create_project(
        self,
        project_name: str,
        parent_directory: str,
        folder_name: str | None = None,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_name = project_name.strip()
        if not project_name:
            raise GodotError("`project_name` is required.")

        parent_dir = Path(parent_directory).expanduser().resolve()
        parent_dir.mkdir(parents=True, exist_ok=True)

        final_folder_name = snake_case_name(folder_name or project_name, default="godot_project")
        project_dir = parent_dir / final_folder_name
        if project_dir.exists():
            contents = list(project_dir.iterdir())
            if contents and not (project_dir / "project.godot").exists():
                raise GodotError(
                    f"{project_dir} already exists and is not an empty Godot project directory."
                )
        project_dir.mkdir(parents=True, exist_ok=True)

        for child in ("scenes", "scripts", "assets"):
            (project_dir / child).mkdir(exist_ok=True)

        project_file = project_dir / "project.godot"
        if not project_file.exists():
            project_file.touch()

        executable, version = resolve_godot_executable(godot_executable)
        _run_godot_script(
            executable=executable,
            project_dir=project_dir,
            script_name="bootstrap_project.gd",
            user_args=["--project-name", project_name],
        )

        return {
            "project_name": project_name,
            "project_path": str(project_dir),
            "project_file": str(project_file),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def create_folder(
        self,
        project_path: str,
        folder_path: str,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        normalized_folder = normalize_project_subdir(folder_path)
        if not normalized_folder:
            raise GodotError("`folder_path` must point to a project-relative directory.")

        absolute_folder = (project_dir / normalized_folder).resolve()
        existed_before = absolute_folder.exists()
        absolute_folder.mkdir(parents=True, exist_ok=True)

        return {
            "project_path": str(project_dir),
            "folder_path": str(absolute_folder),
            "folder_resource_path": f"res://{normalized_folder}",
            "created": not existed_before,
        }

    def get_project_structure(
        self,
        project_path: str,
        folder_path: str | None = None,
        max_depth: int = 6,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        if max_depth < 0:
            raise GodotError("`max_depth` must be 0 or greater.")

        root_dir, root_resource_path = resolve_project_directory_path(project_dir, folder_path)
        entries, directory_count, file_count = _collect_project_entries(
            project_dir=project_dir,
            current_dir=root_dir,
            max_depth=max_depth,
            include_hidden=include_hidden,
        )

        return {
            "project_path": str(project_dir),
            "root_path": str(root_dir),
            "root_resource_path": root_resource_path,
            "max_depth": max_depth,
            "include_hidden": include_hidden,
            "directory_count": directory_count,
            "file_count": file_count,
            "entries": entries,
            "tree_text": _render_project_tree(root_resource_path, entries),
        }

    def list_resources(
        self,
        project_path: str,
        folder_path: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        root_dir, root_resource_path = resolve_project_directory_path(project_dir, folder_path)
        categorized = _scan_project_resources(project_dir, root_dir)
        resource_counts = {
            category: len(entries)
            for category, entries in categorized.items()
        }

        return {
            "project_path": str(project_dir),
            "root_path": str(root_dir),
            "root_resource_path": root_resource_path,
            "resource_counts": resource_counts,
            "total_count": sum(resource_counts.values()),
            **categorized,
        }

    def start_project(
        self,
        project_path: str,
        godot_executable: str | None = None,
        scene_path: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)

        command = [str(executable), "--path", str(project_dir), "-e"]
        opened_scene = None
        if scene_path:
            absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)
            command.append(str(absolute_scene_path))
            opened_scene = resource_scene_path

        launched = _launch_process(command, cwd=project_dir, log_name="start-project")
        return {
            "project_path": str(project_dir),
            "pid": launched.pid,
            "command": launched.command,
            "log_path": launched.log_path,
            "godot_version": version,
            "opened_scene": opened_scene,
        }

    def run_project(
        self,
        project_path: str,
        godot_executable: str | None = None,
        headless: bool = False,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)

        command = [str(executable), "--path", str(project_dir)]
        if headless:
            command.append("--headless")

        launched = _launch_process(command, cwd=project_dir, log_name="run-project")
        return {
            "project_path": str(project_dir),
            "pid": launched.pid,
            "command": launched.command,
            "log_path": launched.log_path,
            "headless": headless,
            "godot_version": version,
        }

    def create_scene(
        self,
        project_path: str,
        scene_name: str,
        root_type: str = "Node2D",
        folder: str = "scenes",
        set_as_main_scene: bool = False,
        overwrite: bool = False,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)

        if not scene_name.strip():
            raise GodotError("`scene_name` is required.")

        normalized_folder = normalize_project_subdir(folder)
        scene_dir = project_dir / normalized_folder if normalized_folder else project_dir
        scene_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{snake_case_name(scene_name)}.tscn"
        absolute_scene_path = (scene_dir / filename).resolve()
        if absolute_scene_path.exists() and not overwrite:
            raise GodotError(f"Scene already exists at {absolute_scene_path}. Pass `overwrite=true` to replace it.")

        relative_scene_path = absolute_scene_path.relative_to(project_dir)
        resource_scene_path = f"res://{relative_scene_path.as_posix()}"
        root_name = pascal_case_name(scene_name)

        _run_godot_script(
            executable=executable,
            project_dir=project_dir,
            script_name="create_scene.gd",
            user_args=[
                "--scene-path",
                resource_scene_path,
                "--root-type",
                root_type,
                "--root-name",
                root_name,
                "--set-main-scene",
                "true" if set_as_main_scene else "false",
            ],
        )

        return {
            "project_path": str(project_dir),
            "scene_name": scene_name,
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "scene_root_name": root_name,
            "root_type": root_type,
            "set_as_main_scene": set_as_main_scene,
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def create_shader(
        self,
        project_path: str,
        shader_name: str,
        folder: str = "shaders",
        shader_type: str = "canvas_item",
        shader_code: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)

        final_shader_name = shader_name.strip()
        if not final_shader_name:
            raise GodotError("`shader_name` is required.")

        final_shader_type = shader_type.strip().lower()
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", final_shader_type):
            raise GodotError("`shader_type` must be a valid Godot shader type name.")

        normalized_folder = normalize_project_subdir(folder)
        shader_dir = project_dir / normalized_folder if normalized_folder else project_dir
        shader_dir.mkdir(parents=True, exist_ok=True)

        filename_base = final_shader_name[:-9] if final_shader_name.lower().endswith(".gdshader") else final_shader_name
        filename = f"{snake_case_name(filename_base, default='shader')}.gdshader"
        shader_path = (shader_dir / filename).resolve()
        existed_before = shader_path.exists()
        if existed_before and not overwrite:
            raise GodotError(
                f"Shader already exists at {shader_path}. Pass `overwrite=true` to replace it."
            )

        shader_source = shader_code if shader_code is not None else _default_shader_source(final_shader_type)
        if not shader_source.endswith("\n"):
            shader_source += "\n"
        shader_path.write_text(shader_source, encoding="utf-8")

        relative_shader_path = shader_path.relative_to(project_dir)
        return {
            "project_path": str(project_dir),
            "shader_name": shader_name,
            "shader_path": str(shader_path),
            "shader_resource_path": f"res://{relative_shader_path.as_posix()}",
            "shader_type": final_shader_type,
            "created_from_template": shader_code is None,
            "known_shader_type": final_shader_type in KNOWN_SHADER_TYPES,
            "created": not existed_before,
        }

    def validate_scene(
        self,
        project_path: str,
        scene_path: str,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        captured = _run_godot_script_with_capture(
            executable=executable,
            project_dir=project_dir,
            script_name="validate_scene.gd",
            user_args=["--scene-path", resource_scene_path],
        )
        if int(captured["returncode"]) != 0:
            details = "\n".join(
                part for part in [str(captured["stdout"]).strip(), str(captured["stderr"]).strip()] if part
            ).strip()
            raise GodotError(
                f"Godot helper script `validate_scene.gd` failed.\n"
                f"Log file: {captured['log_path']}\n"
                f"{details or 'No output was returned.'}"
            )

        parsed = _parse_script_json_output(str(captured["stdout"]), "validate_scene.gd")
        debug_output = _classify_debug_lines(
            str(captured["stdout"]),
            str(captured["stderr"]),
            str(captured["log_output"]),
        )
        filtered_errors = _filter_scene_validation_errors(
            debug_output["errors"],
            resource_scene_path,
            keep_fallback=not bool(parsed.get("valid", False)),
        )

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "valid": bool(parsed.get("valid", False)),
            "message": parsed.get("message"),
            "resource_type": parsed.get("resource_type"),
            "node_count": parsed.get("node_count"),
            "root_node_name": parsed.get("root_node_name"),
            "root_node_type": parsed.get("root_node_type"),
            "connection_count": parsed.get("connection_count"),
            "errors": filtered_errors,
            "warnings": debug_output["warnings"],
            "error_count": len(filtered_errors),
            "warning_count": debug_output["warning_count"],
            "log_path": captured["log_path"],
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def get_scene_tree(
        self,
        project_path: str,
        scene_path: str,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        output = _run_godot_script(
            executable=executable,
            project_dir=project_dir,
            script_name="inspect_scene.gd",
            user_args=["--scene-path", resource_scene_path],
        )
        parsed = _parse_script_json_output(output, "inspect_scene.gd")
        nodes = parsed.get("nodes")
        connections = parsed.get("connections", [])
        if not isinstance(nodes, list):
            raise GodotError("Scene inspection did not return a node list.")
        if not isinstance(connections, list):
            raise GodotError("Scene inspection did not return a connection list.")

        normalized_nodes: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            normalized_node = dict(node)
            normalized_node["path"] = canonical_scene_node_path(str(node.get("path", ".")), ".")
            normalized_node["parent_path"] = canonical_scene_node_path(str(node.get("parent_path", "")), "")
            normalized_node["owner_path"] = canonical_scene_node_path(str(node.get("owner_path", "")), "")
            normalized_nodes.append(normalized_node)

        normalized_connections: list[dict[str, Any]] = []
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            normalized_connection = dict(connection)
            normalized_connection["source_path"] = canonical_scene_node_path(
                str(connection.get("source_path", "")),
                "",
            )
            normalized_connection["target_path"] = canonical_scene_node_path(
                str(connection.get("target_path", "")),
                "",
            )
            normalized_connections.append(normalized_connection)

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "node_count": len(normalized_nodes),
            "nodes": normalized_nodes,
            "scene_tree": _build_scene_tree(normalized_nodes),
            "connections": normalized_connections,
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def add_node(
        self,
        project_path: str,
        scene_path: str,
        node_type: str,
        parent_path: str = ".",
        node_name: str | None = None,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        final_node_type = node_type.strip()
        if not final_node_type:
            raise GodotError("`node_type` is required.")

        final_node_name = (node_name or "").strip() or final_node_type
        normalized_parent_path = normalize_scene_node_path(parent_path)

        output = _run_godot_script(
            executable=executable,
            project_dir=project_dir,
            script_name="add_node.gd",
            user_args=[
                "--scene-path",
                resource_scene_path,
                "--parent-path",
                normalized_parent_path,
                "--node-type",
                final_node_type,
                "--node-name",
                final_node_name,
            ],
        )
        parsed = _parse_script_json_output(output, "add_node.gd")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "parent_path": parsed.get("parent_path", normalized_parent_path),
            "node_path": parsed.get("node_path"),
            "node_name": parsed.get("node_name", final_node_name),
            "node_type": parsed.get("node_type", final_node_type),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def add_world_environment(
        self,
        project_path: str,
        scene_path: str,
        parent_path: str = ".",
        node_name: str = "WorldEnvironment",
        environment_parameters: dict[str, Any] | None = None,
        node_parameters: dict[str, Any] | None = None,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        normalized_parent_path = normalize_scene_node_path(parent_path)
        final_node_name = node_name.strip() if node_name is not None else ""
        if not final_node_name:
            raise GodotError("`node_name` must not be empty.")

        final_environment_parameters = environment_parameters or {}
        if not isinstance(final_environment_parameters, dict):
            raise GodotError("`environment_parameters` must be an object when provided.")
        final_node_parameters = node_parameters or {}
        if not isinstance(final_node_parameters, dict):
            raise GodotError("`node_parameters` must be an object when provided.")

        payload = {
            "environment_parameters": final_environment_parameters,
            "node_parameters": final_node_parameters,
        }
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix="-godot-world-environment-add.json",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                config_path = handle.name
        except TypeError as exc:
            raise GodotError(
                "`environment_parameters` or `node_parameters` contains a value that could not be serialized to JSON."
            ) from exc

        try:
            output = _run_godot_script(
                executable=executable,
                project_dir=project_dir,
                script_name="add_world_environment.gd",
                user_args=[
                    "--scene-path",
                    resource_scene_path,
                    "--parent-path",
                    normalized_parent_path,
                    "--node-name",
                    final_node_name,
                    "--config-path",
                    config_path,
                ],
            )
        finally:
            Path(config_path).unlink(missing_ok=True)

        parsed = _parse_script_json_output(output, "add_world_environment.gd")
        supported_node_parameters = parsed.get("supported_node_parameters", [])
        supported_environment_parameters = parsed.get("supported_environment_parameters", [])
        if not isinstance(supported_node_parameters, list):
            raise GodotError("WorldEnvironment creation did not return a supported node parameter list.")
        if not isinstance(supported_environment_parameters, list):
            raise GodotError("WorldEnvironment creation did not return a supported environment parameter list.")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "parent_path": parsed.get("parent_path", normalized_parent_path),
            "node_path": parsed.get("node_path"),
            "node_name": parsed.get("node_name", final_node_name),
            "node_type": parsed.get("node_type", "WorldEnvironment"),
            "environment_created": bool(parsed.get("environment_created", True)),
            "node_parameters": parsed.get("node_parameters", {}),
            "environment_parameters": parsed.get("environment_parameters", {}),
            "updated_node_parameters": parsed.get("updated_node_parameters", []),
            "updated_environment_parameters": parsed.get("updated_environment_parameters", []),
            "supported_node_parameters": supported_node_parameters,
            "supported_environment_parameters": supported_environment_parameters,
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def update_world_environment(
        self,
        project_path: str,
        scene_path: str,
        node_path: str,
        environment_parameters: dict[str, Any] | None = None,
        node_parameters: dict[str, Any] | None = None,
        create_environment_if_missing: bool = True,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        normalized_node_path = normalize_scene_node_path(node_path)
        final_environment_parameters = environment_parameters or {}
        if not isinstance(final_environment_parameters, dict):
            raise GodotError("`environment_parameters` must be an object when provided.")
        final_node_parameters = node_parameters or {}
        if not isinstance(final_node_parameters, dict):
            raise GodotError("`node_parameters` must be an object when provided.")
        if not final_environment_parameters and not final_node_parameters:
            raise GodotError("Provide at least one of `environment_parameters` or `node_parameters`.")

        payload = {
            "environment_parameters": final_environment_parameters,
            "node_parameters": final_node_parameters,
            "create_environment_if_missing": bool(create_environment_if_missing),
        }
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix="-godot-world-environment-update.json",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                config_path = handle.name
        except TypeError as exc:
            raise GodotError(
                "`environment_parameters` or `node_parameters` contains a value that could not be serialized to JSON."
            ) from exc

        try:
            output = _run_godot_script(
                executable=executable,
                project_dir=project_dir,
                script_name="update_world_environment.gd",
                user_args=[
                    "--scene-path",
                    resource_scene_path,
                    "--node-path",
                    normalized_node_path,
                    "--config-path",
                    config_path,
                ],
            )
        finally:
            Path(config_path).unlink(missing_ok=True)

        parsed = _parse_script_json_output(output, "update_world_environment.gd")
        supported_node_parameters = parsed.get("supported_node_parameters", [])
        supported_environment_parameters = parsed.get("supported_environment_parameters", [])
        if not isinstance(supported_node_parameters, list):
            raise GodotError("WorldEnvironment update did not return a supported node parameter list.")
        if not isinstance(supported_environment_parameters, list):
            raise GodotError("WorldEnvironment update did not return a supported environment parameter list.")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "node_path": parsed.get("node_path", normalized_node_path),
            "node_name": parsed.get("node_name"),
            "node_type": parsed.get("node_type", "WorldEnvironment"),
            "environment_created": bool(parsed.get("environment_created", False)),
            "node_parameters": parsed.get("node_parameters", {}),
            "environment_parameters": parsed.get("environment_parameters", {}),
            "updated_node_parameters": parsed.get("updated_node_parameters", []),
            "updated_environment_parameters": parsed.get("updated_environment_parameters", []),
            "supported_node_parameters": supported_node_parameters,
            "supported_environment_parameters": supported_environment_parameters,
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def add_primitive_mesh(
        self,
        project_path: str,
        scene_path: str,
        mesh_type: str,
        parent_path: str = ".",
        node_name: str | None = None,
        mesh_parameters: dict[str, Any] | None = None,
        transform: dict[str, Any] | None = None,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        final_mesh_type = mesh_type.strip()
        if not final_mesh_type:
            raise GodotError("`mesh_type` is required.")

        normalized_parent_path = normalize_scene_node_path(parent_path)
        final_node_name = (node_name or "").strip()
        if not final_node_name:
            base_name = final_mesh_type[:-4] if final_mesh_type.lower().endswith("mesh") else final_mesh_type
            final_node_name = pascal_case_name(base_name, default="Mesh")

        final_mesh_parameters = mesh_parameters or {}
        if not isinstance(final_mesh_parameters, dict):
            raise GodotError("`mesh_parameters` must be an object when provided.")
        final_transform = transform or {}
        if not isinstance(final_transform, dict):
            raise GodotError("`transform` must be an object when provided.")

        payload = {
            "mesh_parameters": final_mesh_parameters,
            "transform": final_transform,
        }
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix="-godot-primitive-mesh.json",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                config_path = handle.name
        except TypeError as exc:
            raise GodotError(
                "`mesh_parameters` or `transform` contains a value that could not be serialized to JSON."
            ) from exc

        try:
            output = _run_godot_script(
                executable=executable,
                project_dir=project_dir,
                script_name="add_primitive_mesh.gd",
                user_args=[
                    "--scene-path",
                    resource_scene_path,
                    "--parent-path",
                    normalized_parent_path,
                    "--node-name",
                    final_node_name,
                    "--mesh-type",
                    final_mesh_type,
                    "--config-path",
                    config_path,
                ],
            )
        finally:
            Path(config_path).unlink(missing_ok=True)

        parsed = _parse_script_json_output(output, "add_primitive_mesh.gd")
        supported_mesh_parameters = parsed.get("supported_mesh_parameters", [])
        if not isinstance(supported_mesh_parameters, list):
            raise GodotError("Primitive mesh creation did not return a supported parameter list.")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "parent_path": parsed.get("parent_path", normalized_parent_path),
            "node_path": parsed.get("node_path"),
            "node_name": parsed.get("node_name", final_node_name),
            "node_type": parsed.get("node_type", "MeshInstance3D"),
            "mesh_type": parsed.get("mesh_type", final_mesh_type),
            "mesh_parameters": parsed.get("mesh_parameters", {}),
            "supported_mesh_parameters": supported_mesh_parameters,
            "updated_mesh_parameters": parsed.get("updated_mesh_parameters", []),
            "transform": parsed.get("transform", {}),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def edit_primitive_mesh(
        self,
        project_path: str,
        scene_path: str,
        node_path: str,
        mesh_type: str | None = None,
        mesh_parameters: dict[str, Any] | None = None,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        normalized_node_path = normalize_scene_node_path(node_path)
        final_mesh_type = (mesh_type or "").strip()
        final_mesh_parameters = mesh_parameters or {}
        if not isinstance(final_mesh_parameters, dict):
            raise GodotError("`mesh_parameters` must be an object when provided.")
        if not final_mesh_type and not final_mesh_parameters:
            raise GodotError("Provide at least one of `mesh_type` or `mesh_parameters`.")

        payload: dict[str, Any] = {
            "mesh_parameters": final_mesh_parameters,
        }
        if final_mesh_type:
            payload["mesh_type"] = final_mesh_type

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix="-godot-edit-primitive-mesh.json",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                config_path = handle.name
        except TypeError as exc:
            raise GodotError("`mesh_parameters` contains a value that could not be serialized to JSON.") from exc

        try:
            output = _run_godot_script(
                executable=executable,
                project_dir=project_dir,
                script_name="edit_primitive_mesh.gd",
                user_args=[
                    "--scene-path",
                    resource_scene_path,
                    "--node-path",
                    normalized_node_path,
                    "--config-path",
                    config_path,
                ],
            )
        finally:
            Path(config_path).unlink(missing_ok=True)

        parsed = _parse_script_json_output(output, "edit_primitive_mesh.gd")
        supported_mesh_parameters = parsed.get("supported_mesh_parameters", [])
        if not isinstance(supported_mesh_parameters, list):
            raise GodotError("Primitive mesh edit did not return a supported parameter list.")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "node_path": parsed.get("node_path", normalized_node_path),
            "node_name": parsed.get("node_name"),
            "node_type": parsed.get("node_type", "MeshInstance3D"),
            "mesh_type_before": parsed.get("mesh_type_before"),
            "mesh_type_after": parsed.get("mesh_type_after"),
            "mesh_parameters": parsed.get("mesh_parameters", {}),
            "supported_mesh_parameters": supported_mesh_parameters,
            "updated_mesh_parameters": parsed.get("updated_mesh_parameters", []),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def edit_scene(
        self,
        project_path: str,
        scene_path: str,
        node_path: str = ".",
        transform: dict[str, Any] | None = None,
        new_name: str | None = None,
        new_parent_path: str | None = None,
        delete: bool = False,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        normalized_node_path = normalize_scene_node_path(node_path)
        changes: dict[str, Any] = {}

        if transform is not None:
            if not isinstance(transform, dict):
                raise GodotError("`transform` must be an object.")
            if not transform:
                raise GodotError("`transform` must contain at least one field to update.")
            changes["transform"] = transform

        if new_name is not None:
            final_new_name = new_name.strip()
            if not final_new_name:
                raise GodotError("`new_name` must not be empty.")
            changes["new_name"] = final_new_name

        if new_parent_path is not None:
            changes["new_parent_path"] = normalize_scene_node_path(new_parent_path)

        if delete:
            if changes:
                raise GodotError("`delete` cannot be combined with rename, reparent, or transform updates.")
            changes["delete"] = True

        if not changes:
            raise GodotError(
                "Provide at least one scene edit: `transform`, `new_name`, `new_parent_path`, or `delete`."
            )

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix="-godot-scene-edit.json",
                delete=False,
            ) as handle:
                json.dump(changes, handle, ensure_ascii=False)
                changes_path = handle.name
        except TypeError as exc:
            raise GodotError("The requested scene edits contain a value that could not be serialized to JSON.") from exc

        try:
            output = _run_godot_script(
                executable=executable,
                project_dir=project_dir,
                script_name="edit_scene.gd",
                user_args=[
                    "--scene-path",
                    resource_scene_path,
                    "--node-path",
                    normalized_node_path,
                    "--changes-path",
                    changes_path,
                ],
            )
        finally:
            Path(changes_path).unlink(missing_ok=True)

        parsed = _parse_script_json_output(output, "edit_scene.gd")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "node_path_before": parsed.get("node_path_before", normalized_node_path),
            "node_path_after": parsed.get("node_path_after"),
            "parent_path_before": parsed.get("parent_path_before"),
            "parent_path_after": parsed.get("parent_path_after"),
            "node_name_before": parsed.get("node_name_before"),
            "node_name_after": parsed.get("node_name_after"),
            "node_type": parsed.get("node_type"),
            "deleted": bool(parsed.get("deleted", False)),
            "applied_changes": parsed.get("applied_changes", []),
            "updated_transform_fields": parsed.get("updated_transform_fields", []),
            "transform_kind": parsed.get("transform_kind"),
            "supported_fields": parsed.get("supported_fields", []),
            "transform": parsed.get("transform", {}),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def get_node_properties(
        self,
        project_path: str,
        scene_path: str,
        node_path: str = ".",
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        normalized_node_path = normalize_scene_node_path(node_path)
        output = _run_godot_script(
            executable=executable,
            project_dir=project_dir,
            script_name="get_node_properties.gd",
            user_args=[
                "--scene-path",
                resource_scene_path,
                "--node-path",
                normalized_node_path,
            ],
        )
        parsed = _parse_script_json_output(output, "get_node_properties.gd")
        properties = parsed.get("properties", [])
        if not isinstance(properties, list):
            raise GodotError("Node property inspection did not return a property list.")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "node_path": parsed.get("node_path", normalized_node_path),
            "node_name": parsed.get("node_name"),
            "node_type": parsed.get("node_type"),
            "property_count": int(parsed.get("property_count", len(properties))),
            "properties": properties,
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def get_node_transform(
        self,
        project_path: str,
        scene_path: str,
        node_path: str = ".",
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        normalized_node_path = normalize_scene_node_path(node_path)
        output = _run_godot_script(
            executable=executable,
            project_dir=project_dir,
            script_name="get_node_transform.gd",
            user_args=[
                "--scene-path",
                resource_scene_path,
                "--node-path",
                normalized_node_path,
            ],
        )
        parsed = _parse_script_json_output(output, "get_node_transform.gd")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "node_path": parsed.get("node_path", normalized_node_path),
            "node_name": parsed.get("node_name"),
            "node_type": parsed.get("node_type"),
            "transform_kind": parsed.get("transform_kind"),
            "supported_fields": parsed.get("supported_fields", []),
            "transform": parsed.get("transform", {}),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def update_node_transform(
        self,
        project_path: str,
        scene_path: str,
        transform: dict[str, Any],
        node_path: str = ".",
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")
        if not isinstance(transform, dict):
            raise GodotError("`transform` must be an object.")
        if not transform:
            raise GodotError("`transform` must contain at least one field to update.")

        normalized_node_path = normalize_scene_node_path(node_path)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix="-godot-node-transform.json",
                delete=False,
            ) as handle:
                json.dump(transform, handle, ensure_ascii=False)
                updates_path = handle.name
        except TypeError as exc:
            raise GodotError("`transform` contains a value that could not be serialized to JSON.") from exc

        try:
            output = _run_godot_script(
                executable=executable,
                project_dir=project_dir,
                script_name="update_node_transform.gd",
                user_args=[
                    "--scene-path",
                    resource_scene_path,
                    "--node-path",
                    normalized_node_path,
                    "--updates-path",
                    updates_path,
                ],
            )
        finally:
            Path(updates_path).unlink(missing_ok=True)

        parsed = _parse_script_json_output(output, "update_node_transform.gd")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "node_path": parsed.get("node_path", normalized_node_path),
            "node_name": parsed.get("node_name"),
            "node_type": parsed.get("node_type"),
            "transform_kind": parsed.get("transform_kind"),
            "supported_fields": parsed.get("supported_fields", []),
            "updated_fields": parsed.get("updated_fields", []),
            "transform": parsed.get("transform", {}),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def attach_script(
        self,
        project_path: str,
        scene_path: str,
        node_path: str = ".",
        script_path: str | None = None,
        script_name: str | None = None,
        folder: str = "scripts",
        script_code: str | None = None,
        overwrite: bool = False,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        normalized_node_path = normalize_scene_node_path(node_path)
        scene_tree = self.get_scene_tree(
            project_path=str(project_dir),
            scene_path=resource_scene_path,
            godot_executable=str(executable),
        )

        node_info = next(
            (node for node in scene_tree["nodes"] if node.get("path") == normalized_node_path),
            None,
        )
        if node_info is None:
            raise GodotError(
                f"Node not found at path `{normalized_node_path}` in scene {resource_scene_path}."
            )

        node_name = str(node_info.get("name", "")).strip() or "Node"
        node_type = str(node_info.get("type", "")).strip() or "Node"

        if script_path:
            absolute_script_path, resource_script_path = resolve_project_file_path(project_dir, script_path)
        else:
            normalized_folder = normalize_project_subdir(folder)
            script_dir = project_dir / normalized_folder if normalized_folder else project_dir
            base_name = (script_name or "").strip() or node_name
            filename_base = base_name[:-3] if base_name.lower().endswith(".gd") else base_name
            filename = f"{snake_case_name(filename_base, default='script')}.gd"
            absolute_script_path = (script_dir / filename).resolve()
            relative_script_path = absolute_script_path.relative_to(project_dir)
            resource_script_path = f"res://{relative_script_path.as_posix()}"

        if absolute_script_path.suffix.lower() != ".gd":
            raise GodotError("Attached scripts must point to a `.gd` file.")

        absolute_script_path.parent.mkdir(parents=True, exist_ok=True)
        existed_before = absolute_script_path.exists()
        wrote_script = False
        created_from_template = False

        if script_code is not None:
            if existed_before and not overwrite:
                raise GodotError(
                    f"Script already exists at {absolute_script_path}. Pass `overwrite=true` to replace it."
                )
            final_script_code = script_code if script_code.endswith("\n") else script_code + "\n"
            absolute_script_path.write_text(final_script_code, encoding="utf-8")
            wrote_script = True
        elif not existed_before:
            absolute_script_path.write_text(_default_script_source(node_type), encoding="utf-8")
            wrote_script = True
            created_from_template = True

        if not absolute_script_path.exists():
            raise GodotError(f"Script not found: {absolute_script_path}")

        output = _run_godot_script(
            executable=executable,
            project_dir=project_dir,
            script_name="attach_script.gd",
            user_args=[
                "--scene-path",
                resource_scene_path,
                "--node-path",
                normalized_node_path,
                "--script-path",
                resource_script_path,
            ],
        )
        parsed = _parse_script_json_output(output, "attach_script.gd")

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "node_path": parsed.get("node_path", normalized_node_path),
            "node_name": parsed.get("node_name", node_name),
            "node_type": parsed.get("node_type", node_type),
            "script_path": str(absolute_script_path),
            "script_resource_path": resource_script_path,
            "created_script": wrote_script and not existed_before,
            "updated_script": wrote_script and existed_before,
            "created_from_template": created_from_template,
            "previous_script_resource_path": parsed.get("previous_script_path", ""),
            "replaced_existing_script": bool(parsed.get("previous_script_path")),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def update_project_settings(
        self,
        project_path: str,
        settings: list[dict[str, Any]],
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)

        if not settings:
            raise GodotError("`settings` must contain at least one setting update.")

        normalized_settings: list[dict[str, Any]] = []
        for index, item in enumerate(settings):
            if not isinstance(item, dict):
                raise GodotError(f"`settings[{index}]` must be an object.")

            setting_name = str(item.get("name", "")).strip()
            if not setting_name:
                raise GodotError(f"`settings[{index}].name` is required.")

            has_value = "value" in item
            has_value_godot = str(item.get("value_godot", "")).strip() != ""
            if has_value == has_value_godot:
                raise GodotError(
                    f"`settings[{index}]` must include exactly one of `value` or `value_godot`."
                )

            normalized_item = {"name": setting_name}
            if has_value_godot:
                normalized_item["value_godot"] = str(item["value_godot"])
            else:
                normalized_item["value"] = item["value"]
            normalized_settings.append(normalized_item)

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix="-godot-project-settings.json",
                delete=False,
            ) as handle:
                json.dump(normalized_settings, handle, ensure_ascii=False)
                updates_path = handle.name
        except TypeError as exc:
            raise GodotError("`settings` contains a value that could not be serialized to JSON.") from exc

        try:
            output = _run_godot_script(
                executable=executable,
                project_dir=project_dir,
                script_name="update_project_settings.gd",
                user_args=["--updates-path", updates_path],
            )
        finally:
            Path(updates_path).unlink(missing_ok=True)

        parsed = _parse_script_json_output(output, "update_project_settings.gd")
        updated_settings = parsed.get("updated_settings")
        if not isinstance(updated_settings, list):
            raise GodotError("Project settings update did not return a settings list.")

        return {
            "project_path": str(project_dir),
            "project_file": str(project_dir / "project.godot"),
            "updated_count": len(updated_settings),
            "updated_settings": updated_settings,
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def search_docs(
        self,
        query: str | None = None,
        class_name: str | None = None,
        member_name: str | None = None,
        member_type: str = "any",
        max_results: int = 8,
        refresh_cache: bool = False,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        executable, version = resolve_godot_executable(godot_executable)

        search_query = (query or "").strip()
        class_filter = (class_name or "").strip()
        member_filter = (member_name or "").strip()
        kind_filter = member_type.strip().lower() or "any"
        if kind_filter not in {"any", "class", "method", "property", "signal", "constant"}:
            raise GodotError(
                "`member_type` must be one of: any, class, method, property, signal, constant."
            )
        if max_results < 1:
            raise GodotError("`max_results` must be at least 1.")
        if not (search_query or class_filter or member_filter):
            raise GodotError("Provide at least one of `query`, `class_name`, or `member_name`.")

        docs_path, docs_api = self._get_docs_api(
            executable=executable,
            version=version,
            refresh_cache=refresh_cache,
        )

        effective_query = search_query or " ".join(part for part in [class_filter, member_filter] if part)
        query_text = _normalize_search_text(effective_query)
        query_tokens = set(_tokenize_search_text(effective_query))
        class_filter_lower = class_filter.lower()
        member_filter_lower = member_filter.lower()

        raw_results: list[tuple[int, dict[str, Any]]] = []

        for cls in docs_api.get("classes", []):
            if not isinstance(cls, dict):
                continue

            class_name_value = str(cls.get("name", "")).strip()
            if not class_name_value:
                continue

            class_name_lower = class_name_value.lower()
            class_exact = bool(class_filter and class_name_lower == class_filter_lower)
            class_partial = bool(class_filter and class_filter_lower in class_name_lower)
            if class_filter and not (class_exact or class_partial):
                continue

            class_brief = str(cls.get("brief_description", ""))
            class_description = str(cls.get("description", ""))
            class_score = _score_identifier(class_name_value, query_text, query_tokens)
            class_score += _score_description(class_brief, query_text, query_tokens)
            class_score += _score_description(class_description, query_text, query_tokens)
            if class_exact:
                class_score += 160
            elif class_partial:
                class_score += 60

            if kind_filter in {"any", "class"} and not member_filter:
                if class_score > 0 or class_exact:
                    raw_results.append(
                        (
                            class_score,
                            {
                                "kind": "class",
                                "class_name": class_name_value,
                                "inherits": cls.get("inherits"),
                                "signature": _format_match_signature("class", class_name_value, cls),
                                "brief_description": _compact_doc_text(class_brief, 180),
                                "description_snippet": _compact_doc_text(class_description),
                            },
                        )
                    )

            collections: list[tuple[str, list[dict[str, Any]]]] = []
            if kind_filter in {"any", "method"}:
                collections.append(("method", cls.get("methods", [])))
            if kind_filter in {"any", "property"}:
                collections.append(("property", cls.get("properties", [])))
            if kind_filter in {"any", "signal"}:
                collections.append(("signal", cls.get("signals", [])))
            if kind_filter in {"any", "constant"}:
                collections.append(("constant", cls.get("constants", [])))

            for kind, items in collections:
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue

                    item_name = str(item.get("name", "")).strip()
                    if not item_name:
                        continue

                    item_name_lower = item_name.lower()
                    member_exact = bool(member_filter and item_name_lower == member_filter_lower)
                    member_partial = bool(member_filter and member_filter_lower in item_name_lower)
                    if member_filter and not (member_exact or member_partial):
                        continue

                    item_description = str(item.get("description", ""))
                    score = _score_identifier(item_name, query_text, query_tokens)
                    score += _score_description(item_description, query_text, query_tokens)
                    if class_exact:
                        score += 40
                    elif class_partial:
                        score += 10
                    if member_exact:
                        score += 180
                    elif member_partial:
                        score += 70

                    if score <= 0 and not member_exact:
                        continue

                    raw_results.append(
                        (
                            score,
                            {
                                "kind": kind,
                                "class_name": class_name_value,
                                "name": item_name,
                                "signature": _format_match_signature(kind, class_name_value, item),
                                "description_snippet": _compact_doc_text(item_description),
                            },
                        )
                    )

        raw_results.sort(key=lambda item: (-item[0], item[1].get("kind", ""), item[1].get("class_name", ""), item[1].get("name", "")))

        unique_results: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for score, result in raw_results:
            key = (
                str(result.get("kind", "")),
                str(result.get("class_name", "")),
                str(result.get("name", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            result["score"] = score
            unique_results.append(result)

        return {
            "query": search_query or None,
            "class_name": class_filter or None,
            "member_name": member_filter or None,
            "member_type": kind_filter,
            "results": unique_results[:max_results],
            "total_matches": len(unique_results),
            "docs_cache_path": str(docs_path),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def run_scene(
        self,
        project_path: str,
        scene_path: str,
        godot_executable: str | None = None,
        headless: bool = False,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)

        if not absolute_scene_path.exists():
            raise GodotError(f"Scene not found: {absolute_scene_path}")

        command = [str(executable), "--path", str(project_dir)]
        if headless:
            command.append("--headless")
        command.append(str(absolute_scene_path))

        launched = _launch_process(command, cwd=project_dir, log_name="run-scene")
        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path),
            "scene_resource_path": resource_scene_path,
            "pid": launched.pid,
            "command": launched.command,
            "log_path": launched.log_path,
            "headless": headless,
            "godot_version": version,
        }

    def screenshot(
        self,
        project_path: str,
        scene_path: str | None = None,
        capture_seconds: float = 2.0,
        fps: int = 60,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)

        if capture_seconds <= 0:
            raise GodotError("`capture_seconds` must be greater than 0.")
        if fps < 1:
            raise GodotError("`fps` must be at least 1.")

        absolute_scene_path: Path | None = None
        resource_scene_path: str | None = None
        run_target = "project"
        scene_label = "project"

        command = [
            str(executable),
            "--path",
            str(project_dir),
            "--windowed",
            "--fixed-fps",
            str(fps),
            "--disable-vsync",
        ]

        if scene_path:
            absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)
            if not absolute_scene_path.exists():
                raise GodotError(f"Scene not found: {absolute_scene_path}")
            relative_scene_path = absolute_scene_path.relative_to(project_dir).as_posix()
            command.extend(["--scene", relative_scene_path])
            run_target = "scene"
            scene_label = absolute_scene_path.stem

        frame_count = max(1, int(round(capture_seconds * fps)))
        actual_capture_seconds = frame_count / float(fps)

        screenshots_dir = project_dir / ".godot-mcp" / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        stem = f"{timestamp}-{run_target}-{snake_case_name(scene_label, default=run_target)}"
        movie_output_path = screenshots_dir / f"{stem}.png"
        final_screenshot_path = screenshots_dir / f"{stem}-screenshot.png"
        log_path = _create_log_path(project_dir, "screenshot")

        command_with_capture = [
            command[0],
            "--log-file",
            str(log_path),
            *command[1:],
            "--write-movie",
            str(movie_output_path),
            "--quit-after",
            str(frame_count),
        ]

        timeout_seconds = max(60, int(actual_capture_seconds * 20) + 30)
        result = subprocess.run(
            command_with_capture,
            cwd=project_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

        frame_files = sorted(screenshots_dir.glob(f"{stem}" + "[0-9]" * 8 + ".png"))
        if not frame_files and movie_output_path.exists():
            frame_files = [movie_output_path]

        if result.returncode != 0 or not frame_files:
            details = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()
            raise GodotError(
                "Failed to capture a Godot screenshot.\n"
                f"Log file: {log_path}\n"
                f"{details or 'No output was returned.'}"
            )

        final_frame = frame_files[-1]
        if final_screenshot_path.exists():
            final_screenshot_path.unlink()
        final_frame.replace(final_screenshot_path)

        for frame_path in frame_files[:-1]:
            frame_path.unlink(missing_ok=True)
        movie_output_path.unlink(missing_ok=True)
        (screenshots_dir / f"{stem}.wav").unlink(missing_ok=True)

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path) if absolute_scene_path is not None else None,
            "scene_resource_path": resource_scene_path,
            "run_target": run_target,
            "capture_seconds": actual_capture_seconds,
            "requested_capture_seconds": capture_seconds,
            "fps": fps,
            "frame_count": frame_count,
            "frame_index": frame_count - 1,
            "screenshot_path": str(final_screenshot_path),
            "image_format": "png",
            "size_bytes": final_screenshot_path.stat().st_size,
            "command": command_with_capture,
            "log_path": str(log_path),
            "godot_executable": str(executable),
            "godot_version": version,
        }

    def _run_profiler(
        self,
        project_path: str,
        scene_path: str | None,
        duration: float,
        sample_interval: float,
        headless: bool,
        godot_executable: str | None,
        stat_keys: list[str],
        include_samples: bool,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        if duration <= 0:
            raise GodotError("`duration` must be greater than 0.")

        mcp_dir = project_dir / ".godot-mcp"
        mcp_dir.mkdir(parents=True, exist_ok=True)
        results_path = mcp_dir / "profiler_results.json"
        results_path.unlink(missing_ok=True)

        script_dest, original_project_content = _inject_profiler_autoload(project_dir)

        try:
            log_path = _create_log_path(project_dir, "profiler")
            command: list[str] = [
                str(executable),
                "--log-file",
                str(log_path),
                "--path",
                str(project_dir),
            ]
            if headless:
                command.append("--headless")

            absolute_scene_path: Path | None = None
            resource_scene_path: str | None = None
            run_target = "project"
            if scene_path:
                absolute_scene_path, resource_scene_path = resolve_scene_path(
                    project_dir, scene_path
                )
                if not absolute_scene_path.exists():
                    raise GodotError(f"Scene not found: {absolute_scene_path}")
                command.append(str(absolute_scene_path))
                run_target = "scene"

            command.extend([
                "--",
                "--duration",
                str(duration),
                "--output-path",
                str(results_path),
                "--sample-interval",
                str(sample_interval),
            ])

            timeout_seconds = max(60, int(duration * 3) + 30)
            process = subprocess.Popen(
                command,
                cwd=project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            terminated_after_timeout = False
            force_killed = False
            try:
                stdout_text, stderr_text = process.communicate(
                    timeout=timeout_seconds
                )
            except subprocess.TimeoutExpired:
                terminated_after_timeout = True
                process.terminate()
                try:
                    stdout_text, stderr_text = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    force_killed = True
                    process.kill()
                    stdout_text, stderr_text = process.communicate(timeout=10)

        finally:
            _remove_profiler_autoload(project_dir, original_project_content, script_dest)

        if not results_path.exists():
            log_text = (
                log_path.read_text(encoding="utf-8", errors="replace")
                if log_path.exists()
                else ""
            )
            debug = _classify_debug_lines(stdout_text, stderr_text, log_text)
            raise GodotError(
                "Profiler did not produce results. The project may have crashed or "
                "failed to start.\n"
                f"Log file: {log_path}\n"
                f"Exit code: {process.returncode}\n"
                + (
                    f"Errors:\n" + "\n".join(debug["errors"][:10])
                    if debug["errors"]
                    else f"stderr: {stderr_text[:2000]}" if stderr_text.strip() else "No error output."
                )
            )

        raw_results = json.loads(
            results_path.read_text(encoding="utf-8", errors="replace")
        )
        results_path.unlink(missing_ok=True)

        samples: list[dict[str, Any]] = raw_results.get("samples", [])
        aggregate = _compute_aggregate_stats(samples, stat_keys)

        result: dict[str, Any] = {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path) if absolute_scene_path else None,
            "scene_resource_path": resource_scene_path,
            "run_target": run_target,
            "duration_seconds": raw_results.get("duration_seconds", duration),
            "sample_count": len(samples),
            "headless": headless,
            "aggregate_stats": aggregate,
            "command": command,
            "log_path": str(log_path),
            "exit_code": process.returncode,
            "terminated_after_timeout": terminated_after_timeout,
            "force_killed": force_killed,
            "godot_version": version,
        }
        if include_samples:
            result["samples"] = samples

        return result

    def run_profiler(
        self,
        project_path: str,
        scene_path: str | None = None,
        duration: float = 5.0,
        sample_interval: float = 0.0,
        headless: bool = False,
        include_samples: bool = False,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        """Run a project/scene with the performance profiler and return aggregate stats.

        Collects FPS, frame times, memory, object counts, and physics metrics
        from Godot's ``Performance`` singleton.
        """
        return self._run_profiler(
            project_path=project_path,
            scene_path=scene_path,
            duration=duration,
            sample_interval=sample_interval,
            headless=headless,
            godot_executable=godot_executable,
            stat_keys=_PERFORMANCE_STAT_KEYS,
            include_samples=include_samples,
        )

    def run_visual_profiler(
        self,
        project_path: str,
        scene_path: str | None = None,
        duration: float = 5.0,
        sample_interval: float = 0.0,
        headless: bool = False,
        include_samples: bool = False,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        """Run a project/scene with the visual profiler and return rendering stats.

        Focuses on draw calls, primitives, objects in frame, and video memory
        from Godot's ``Performance`` singleton.
        """
        return self._run_profiler(
            project_path=project_path,
            scene_path=scene_path,
            duration=duration,
            sample_interval=sample_interval,
            headless=headless,
            godot_executable=godot_executable,
            stat_keys=_VISUAL_STAT_KEYS,
            include_samples=include_samples,
        )

    def run_with_capture(
        self,
        project_path: str,
        scene_path: str | None = None,
        godot_executable: str | None = None,
        headless: bool = False,
        capture_seconds: float = 3.0,
        max_output_chars: int = 12000,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)
        if capture_seconds <= 0:
            raise GodotError("`capture_seconds` must be greater than 0.")

        command = [str(executable), "--log-file", str(_create_log_path(project_dir, "run-capture")), "--path", str(project_dir)]
        absolute_scene_path: Path | None = None
        resource_scene_path: str | None = None
        run_target = "project"
        if headless:
            command.append("--headless")
        if scene_path:
            absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)
            if not absolute_scene_path.exists():
                raise GodotError(f"Scene not found: {absolute_scene_path}")
            command.append(str(absolute_scene_path))
            run_target = "scene"

        log_path = Path(command[2])
        process = subprocess.Popen(
            command,
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        terminated_after_capture = False
        force_killed = False
        try:
            stdout_text, stderr_text = process.communicate(timeout=capture_seconds)
        except subprocess.TimeoutExpired:
            terminated_after_capture = True
            process.terminate()
            try:
                stdout_text, stderr_text = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                force_killed = True
                process.kill()
                stdout_text, stderr_text = process.communicate(timeout=5)

        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        stdout_excerpt, stdout_truncated = _truncate_text(stdout_text, max_output_chars)
        stderr_excerpt, stderr_truncated = _truncate_text(stderr_text, max_output_chars)
        log_excerpt, log_truncated = _truncate_text(log_text, max_output_chars)
        debug_output = _classify_debug_lines(stdout_text, stderr_text, log_text)

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path) if absolute_scene_path is not None else None,
            "scene_resource_path": resource_scene_path,
            "run_target": run_target,
            "command": command,
            "log_path": str(log_path),
            "headless": headless,
            "capture_seconds": capture_seconds,
            "exit_code": process.returncode,
            "terminated_after_capture": terminated_after_capture,
            "force_killed": force_killed,
            "stdout": stdout_excerpt,
            "stderr": stderr_excerpt,
            "log_output": log_excerpt,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "log_output_truncated": log_truncated,
            "debug_output": debug_output,
            "godot_version": version,
        }

    def record_video(
        self,
        project_path: str,
        scene_path: str | None = None,
        duration: float = 5.0,
        fps: int = 30,
        resolution: dict[str, int] | None = None,
        camera_waypoints: list[dict[str, Any]] | None = None,
        camera_node_path: str | None = None,
        godot_executable: str | None = None,
    ) -> dict[str, Any]:
        project_dir = ensure_project_path(project_path)
        executable, version = resolve_godot_executable(godot_executable)

        if duration <= 0:
            raise GodotError("`duration` must be greater than 0.")
        if fps < 1:
            raise GodotError("`fps` must be at least 1.")

        ffmpeg_path = shutil.which("ffmpeg")

        absolute_scene_path: Path | None = None
        resource_scene_path: str | None = None
        run_target = "project"
        scene_label = "project"

        command: list[str] = [
            str(executable),
            "--path",
            str(project_dir),
            "--windowed",
            "--fixed-fps",
            str(fps),
            "--disable-vsync",
        ]

        if scene_path:
            absolute_scene_path, resource_scene_path = resolve_scene_path(project_dir, scene_path)
            if not absolute_scene_path.exists():
                raise GodotError(f"Scene not found: {absolute_scene_path}")
            relative_scene_path = absolute_scene_path.relative_to(project_dir).as_posix()
            command.extend(["--scene", relative_scene_path])
            run_target = "scene"
            scene_label = absolute_scene_path.stem

        # Apply custom resolution
        if resolution:
            width = resolution.get("width", 1920)
            height = resolution.get("height", 1080)
            command.extend([
                "--resolution",
                f"{width}x{height}",
            ])

        frame_count = max(1, int(round(duration * fps)))

        recordings_dir = project_dir / ".godot-mcp" / "recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        stem = f"{timestamp}-{run_target}-{snake_case_name(scene_label, default=run_target)}"
        movie_output_path = recordings_dir / f"{stem}.png"
        final_video_path = recordings_dir / f"{stem}.mp4"
        log_path = _create_log_path(project_dir, "record-video")

        injected_autoload = False
        original_project_content: str | None = None
        camera_script_dest: Path | None = None
        waypoints_file_path: str | None = None

        try:
            if camera_waypoints:
                camera_script_dest, original_project_content = _inject_camera_controller_autoload(
                    project_dir
                )
                injected_autoload = True

                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix="-godot-camera-waypoints.json",
                    delete=False,
                ) as handle:
                    json.dump(camera_waypoints, handle, ensure_ascii=False)
                    waypoints_file_path = handle.name

            command_with_capture = [
                command[0],
                "--log-file",
                str(log_path),
                *command[1:],
                "--write-movie",
                str(movie_output_path),
                "--quit-after",
                str(frame_count),
            ]

            if camera_waypoints:
                camera_args = [
                    "--",
                    "--camera-duration",
                    str(duration),
                ]
                if waypoints_file_path:
                    camera_args.extend(["--waypoints-path", waypoints_file_path])
                if camera_node_path:
                    camera_args.extend(["--camera-node-path", camera_node_path])
                command_with_capture.extend(camera_args)

            timeout_seconds = max(120, int(duration * 20) + 60)
            result = subprocess.run(
                command_with_capture,
                cwd=project_dir,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )

        finally:
            if injected_autoload and original_project_content is not None and camera_script_dest is not None:
                _remove_camera_controller_autoload(project_dir, original_project_content, camera_script_dest)
            if waypoints_file_path:
                Path(waypoints_file_path).unlink(missing_ok=True)

        frame_files = sorted(recordings_dir.glob(f"{stem}" + "[0-9]" * 8 + ".png"))
        if not frame_files and movie_output_path.exists():
            frame_files = [movie_output_path]

        wav_path = recordings_dir / f"{stem}.wav"

        if result.returncode != 0 or not frame_files:
            details = "\n".join(
                part for part in [result.stdout.strip(), result.stderr.strip()] if part
            ).strip()
            raise GodotError(
                "Failed to capture video frames from Godot.\n"
                f"Log file: {log_path}\n"
                f"{details or 'No output was returned.'}"
            )

        has_audio = wav_path.exists() and wav_path.stat().st_size > 0
        warnings: list[str] = []

        if ffmpeg_path is None:
            warnings.append(
                "ffmpeg was not found on PATH. "
                "Skipping video encoding. Raw frames have been kept. "
                
            )
            frame_paths = [str(f) for f in frame_files]

            return {
                "project_path": str(project_dir),
                "scene_path": str(absolute_scene_path) if absolute_scene_path is not None else None,
                "scene_resource_path": resource_scene_path,
                "run_target": run_target,
                "duration": duration,
                "fps": fps,
                "frame_count": frame_count,
                "output_format": "frames",
                "frame_paths": frame_paths,
                "frame_dir": str(recordings_dir),
                "audio_path": str(wav_path) if has_audio else None,
                "has_audio": has_audio,
                "camera_waypoints_count": len(camera_waypoints) if camera_waypoints else 0,
                "command": command_with_capture,
                "log_path": str(log_path),
                "godot_executable": str(executable),
                "godot_version": version,
                "warnings": warnings,
            }

        frame_pattern = str(recordings_dir / f"{stem}%08d.png")

        ffmpeg_command: list[str] = [
            ffmpeg_path,
            "-y",
            "-framerate",
            str(fps),
            "-i",
            frame_pattern,
        ]

        if has_audio:
            ffmpeg_command.extend([
                "-i",
                str(wav_path),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
            ])
        else:
            ffmpeg_command.extend([
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
            ])

        ffmpeg_command.append(str(final_video_path))

        ffmpeg_result = subprocess.run(
            ffmpeg_command,
            cwd=recordings_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if ffmpeg_result.returncode != 0:
            details = ffmpeg_result.stderr.strip()
            raise GodotError(
                "ffmpeg failed to encode the video.\n"
                f"{details or 'No output was returned.'}"
            )

        for frame_path in frame_files:
            frame_path.unlink(missing_ok=True)
        movie_output_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)

        video_size = final_video_path.stat().st_size if final_video_path.exists() else 0

        return {
            "project_path": str(project_dir),
            "scene_path": str(absolute_scene_path) if absolute_scene_path is not None else None,
            "scene_resource_path": resource_scene_path,
            "run_target": run_target,
            "duration": duration,
            "fps": fps,
            "frame_count": frame_count,
            "output_format": "mp4",
            "video_path": str(final_video_path),
            "video_size_bytes": video_size,
            "has_audio": has_audio,
            "camera_waypoints_count": len(camera_waypoints) if camera_waypoints else 0,
            "command": command_with_capture,
            "log_path": str(log_path),
            "godot_executable": str(executable),
            "godot_version": version,
            "warnings": warnings,
        }


def format_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, sort_keys=True)
