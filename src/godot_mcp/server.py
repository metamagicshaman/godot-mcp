from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from godot_mcp import __version__
from godot_mcp.godot import GodotController, GodotError, format_tool_result
from godot_mcp.protocol import JsonRpcError, read_message, write_message


SUPPORTED_PROTOCOL_VERSION = "2024-11-05"


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(slots=True)
class ResourceDefinition:
    uri: str
    name: str
    description: str
    mime_type: str
    handler: Callable[[], str]


@dataclass(slots=True)
class ResourceTemplateDefinition:
    uri_template: str
    name: str
    description: str
    mime_type: str


class GodotMcpServer:
    def __init__(self) -> None:
        self.controller = GodotController()
        self._initialized = False
        self._tools = {tool.name: tool for tool in self._build_tools()}
        self._resources = {resource.uri: resource for resource in self._build_resources()}
        self._resource_templates = self._build_resource_templates()

    def _build_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="godot_detect_executable",
                description="Resolve the Godot 4.5+ executable that this MCP server will use.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        }
                    },
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.detect_executable(
                    godot_executable=args.get("godot_executable")
                ),
            ),
            ToolDefinition(
                name="godot_create_project",
                description="Create a new Godot project, create common folders, and set the project display name.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "Display name for the Godot project.",
                        },
                        "parent_directory": {
                            "type": "string",
                            "description": "Directory where the new project folder should be created.",
                        },
                        "folder_name": {
                            "type": "string",
                            "description": "Optional folder name override. Defaults to a snake_case form of project_name.",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_name", "parent_directory"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.create_project(
                    project_name=args["project_name"],
                    parent_directory=args["parent_directory"],
                    folder_name=args.get("folder_name"),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_create_folder",
                description="Create a safe project-relative folder inside an existing Godot project.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "folder_path": {
                            "type": "string",
                            "description": "Project-relative folder path to create, such as shaders, scenes/ui, or assets/enemies/boss.",
                        },
                    },
                    "required": ["project_path", "folder_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.create_folder(
                    project_path=args["project_path"],
                    folder_path=args["folder_path"],
                ),
            ),
            ToolDefinition(
                name="godot_get_project_structure",
                description="Return a nested folder/file view of a Godot project or one of its subfolders.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "folder_path": {
                            "type": "string",
                            "description": "Optional absolute, relative, or res:// path for the subfolder to inspect. Defaults to the project root.",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": "Maximum folder depth to expand below the chosen root.",
                            "default": 6,
                            "minimum": 0,
                        },
                        "include_hidden": {
                            "type": "boolean",
                            "description": "Whether to include hidden files and folders such as `.godot` or `.godot-mcp`.",
                            "default": False,
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.get_project_structure(
                    project_path=args["project_path"],
                    folder_path=args.get("folder_path"),
                    max_depth=int(args.get("max_depth", 6)),
                    include_hidden=bool(args.get("include_hidden", False)),
                ),
            ),
            ToolDefinition(
                name="godot_list_resources",
                description="List saved scripts, shaders, scenes, and textures in a Godot project or one of its subfolders.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "folder_path": {
                            "type": "string",
                            "description": "Optional absolute, relative, or res:// path for the subfolder to scan. Defaults to the project root.",
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.list_resources(
                    project_path=args["project_path"],
                    folder_path=args.get("folder_path"),
                ),
            ),
            ToolDefinition(
                name="godot_start_project",
                description="Start Godot's editor for an existing project, optionally opening a specific scene in the editor.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Optional .tscn path to open in the editor.",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.start_project(
                    project_path=args["project_path"],
                    scene_path=args.get("scene_path"),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_run_project",
                description="Run a Godot project using its current main scene.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "headless": {
                            "type": "boolean",
                            "description": "Whether to run the project in headless mode.",
                            "default": False,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.run_project(
                    project_path=args["project_path"],
                    headless=bool(args.get("headless", False)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_create_scene",
                description="Create and save a new scene with a normalized Godot-style filename and root node name.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_name": {
                            "type": "string",
                            "description": "Human-friendly scene name, for example 'Main Menu'.",
                        },
                        "root_type": {
                            "type": "string",
                            "description": "Godot node type for the scene root, such as Node2D, Control, or Node3D.",
                            "default": "Node2D",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Project-relative folder where the scene should be saved.",
                            "default": "scenes",
                        },
                        "set_as_main_scene": {
                            "type": "boolean",
                            "description": "Whether to update the project so this scene becomes the main scene.",
                            "default": False,
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "Whether to replace an existing scene file with the same normalized name.",
                            "default": False,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.create_scene(
                    project_path=args["project_path"],
                    scene_name=args["scene_name"],
                    root_type=args.get("root_type", "Node2D"),
                    folder=args.get("folder", "scenes"),
                    set_as_main_scene=bool(args.get("set_as_main_scene", False)),
                    overwrite=bool(args.get("overwrite", False)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_create_shader",
                description="Create a `.gdshader` file inside the Godot project, either from a starter template or explicit shader code.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "shader_name": {
                            "type": "string",
                            "description": "Human-friendly shader name or filename, for example 'Water Ripple' or 'water_ripple.gdshader'.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Project-relative folder where the shader should be saved.",
                            "default": "shaders",
                        },
                        "shader_type": {
                            "type": "string",
                            "description": "Shader type for the generated template, such as canvas_item, spatial, particles, sky, or fog.",
                            "default": "canvas_item",
                        },
                        "shader_code": {
                            "type": "string",
                            "description": "Optional explicit shader source. When omitted, a starter template is generated.",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "Whether to replace an existing shader file with the same normalized name.",
                            "default": False,
                        },
                    },
                    "required": ["project_path", "shader_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.create_shader(
                    project_path=args["project_path"],
                    shader_name=args["shader_name"],
                    folder=args.get("folder", "shaders"),
                    shader_type=args.get("shader_type", "canvas_item"),
                    shader_code=args.get("shader_code"),
                    overwrite=bool(args.get("overwrite", False)),
                ),
            ),
            ToolDefinition(
                name="godot_update_project_settings",
                description="Update one or more ProjectSettings values and save them back to the Godot project through Godot itself.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "settings": {
                            "type": "array",
                            "description": "List of setting updates. Each item must include `name` plus exactly one of `value` or `value_godot`.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {
                                        "type": "string",
                                        "description": "Project setting path, such as application/config/name or display/window/size/viewport_width.",
                                    },
                                    "value": {
                                        "description": "JSON-compatible value to save into ProjectSettings.",
                                    },
                                    "value_godot": {
                                        "type": "string",
                                        "description": "Optional raw Godot expression, such as Vector2i(1280, 720) or Color(1, 0, 0).",
                                    },
                                },
                                "required": ["name"],
                                "additionalProperties": False,
                            },
                            "minItems": 1,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "settings"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.update_project_settings(
                    project_path=args["project_path"],
                    settings=args["settings"],
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_attach_script",
                description="Attach a GDScript file to a specific node in a saved scene, creating the script file first when needed.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "node_path": {
                            "type": "string",
                            "description": "Scene-relative node path to receive the script. Use '.' for the root node.",
                            "default": ".",
                        },
                        "script_path": {
                            "type": "string",
                            "description": "Optional absolute, relative, or res:// path for the target `.gd` file inside the project.",
                        },
                        "script_name": {
                            "type": "string",
                            "description": "Optional human-friendly script name or filename when creating a new script, such as 'Hero Controller' or 'hero_controller.gd'.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Project-relative folder where a new script should be created when `script_path` is omitted.",
                            "default": "scripts",
                        },
                        "script_code": {
                            "type": "string",
                            "description": "Optional explicit GDScript source. When omitted and the script does not exist yet, a starter template is generated.",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "Whether to replace an existing script file when `script_code` is provided.",
                            "default": False,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.attach_script(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    node_path=args.get("node_path", "."),
                    script_path=args.get("script_path"),
                    script_name=args.get("script_name"),
                    folder=args.get("folder", "scripts"),
                    script_code=args.get("script_code"),
                    overwrite=bool(args.get("overwrite", False)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_get_scene_tree",
                description="Inspect a saved scene and return its node hierarchy plus parent/child relationships.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.get_scene_tree(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_validate_scene",
                description="Dry-run load a saved scene resource and report whether Godot can parse it successfully, without running the scene.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.validate_scene(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_add_node",
                description="Add a new node of any instantiable Godot node type to an existing saved scene.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "node_type": {
                            "type": "string",
                            "description": "Godot node class to instantiate, such as Sprite2D, Timer, Control, or AudioStreamPlayer2D.",
                        },
                        "parent_path": {
                            "type": "string",
                            "description": "Scene-relative node path where the new node should be attached. Use '.' for the root.",
                            "default": ".",
                        },
                        "node_name": {
                            "type": "string",
                            "description": "Optional explicit node name. Defaults to the chosen node type.",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path", "node_type"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.add_node(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    node_type=args["node_type"],
                    parent_path=args.get("parent_path", "."),
                    node_name=args.get("node_name"),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_add_world_environment",
                description="Add a WorldEnvironment node to a saved scene and optionally set node and Environment parameters in one call.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "parent_path": {
                            "type": "string",
                            "description": "Scene-relative node path where the WorldEnvironment node should be attached. Use '.' for the root.",
                            "default": ".",
                        },
                        "node_name": {
                            "type": "string",
                            "description": "Optional explicit WorldEnvironment node name. Defaults to 'WorldEnvironment'.",
                            "default": "WorldEnvironment",
                        },
                        "environment_parameters": {
                            "type": "object",
                            "description": "Optional Environment property updates to apply, such as background_color, ambient_light_energy, tonemap_mode, or fog_enabled.",
                        },
                        "node_parameters": {
                            "type": "object",
                            "description": "Optional WorldEnvironment node property updates (excluding the `environment` resource itself).",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.add_world_environment(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    parent_path=args.get("parent_path", "."),
                    node_name=args.get("node_name", "WorldEnvironment"),
                    environment_parameters=args.get("environment_parameters"),
                    node_parameters=args.get("node_parameters"),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_update_world_environment",
                description="Update an existing WorldEnvironment node by editing its node properties and/or Environment resource parameters.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "node_path": {
                            "type": "string",
                            "description": "Scene-relative path to the WorldEnvironment node to update.",
                        },
                        "environment_parameters": {
                            "type": "object",
                            "description": "Environment property updates to apply, such as background_color, ambient_light_energy, tonemap_mode, or fog_enabled.",
                        },
                        "node_parameters": {
                            "type": "object",
                            "description": "WorldEnvironment node property updates (excluding the `environment` resource itself).",
                        },
                        "create_environment_if_missing": {
                            "type": "boolean",
                            "description": "Whether to create a new Environment resource when the target node has none.",
                            "default": True,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path", "node_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.update_world_environment(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    node_path=args["node_path"],
                    environment_parameters=args.get("environment_parameters"),
                    node_parameters=args.get("node_parameters"),
                    create_environment_if_missing=bool(args.get("create_environment_if_missing", True)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_add_primitive_mesh",
                description="Add a MeshInstance3D with a built-in PrimitiveMesh resource such as BoxMesh, CylinderMesh, SphereMesh, CapsuleMesh, PlaneMesh, PrismMesh, QuadMesh, or TorusMesh.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "mesh_type": {
                            "type": "string",
                            "description": "Primitive mesh class to instantiate, such as BoxMesh, CylinderMesh, SphereMesh, CapsuleMesh, PlaneMesh, PrismMesh, QuadMesh, or TorusMesh.",
                        },
                        "parent_path": {
                            "type": "string",
                            "description": "Scene-relative node path where the new mesh node should be attached. Use '.' for the root.",
                            "default": ".",
                        },
                        "node_name": {
                            "type": "string",
                            "description": "Optional explicit MeshInstance3D node name. Defaults to a PascalCase form of the mesh type without a trailing Mesh suffix.",
                        },
                        "mesh_parameters": {
                            "type": "object",
                            "description": "Optional primitive mesh property overrides such as size, radius, height, or segment counts.",
                        },
                        "transform": {
                            "type": "object",
                            "description": "Optional Node3D transform fields to apply to the new MeshInstance3D. Vector values can be passed as objects like {x, y, z}.",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path", "mesh_type"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.add_primitive_mesh(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    mesh_type=args["mesh_type"],
                    parent_path=args.get("parent_path", "."),
                    node_name=args.get("node_name"),
                    mesh_parameters=args.get("mesh_parameters"),
                    transform=args.get("transform"),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_edit_primitive_mesh",
                description="Modify the PrimitiveMesh resource attached to an existing MeshInstance3D by changing its mesh type and/or one or more mesh parameters.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "node_path": {
                            "type": "string",
                            "description": "Scene-relative node path to the MeshInstance3D node that should be edited.",
                        },
                        "mesh_type": {
                            "type": "string",
                            "description": "Optional new PrimitiveMesh class such as BoxMesh, CylinderMesh, SphereMesh, CapsuleMesh, PlaneMesh, PrismMesh, QuadMesh, or TorusMesh.",
                        },
                        "mesh_parameters": {
                            "type": "object",
                            "description": "Primitive mesh property overrides such as size, radius, height, or segment counts.",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path", "node_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.edit_primitive_mesh(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    node_path=args["node_path"],
                    mesh_type=args.get("mesh_type"),
                    mesh_parameters=args.get("mesh_parameters"),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_edit_scene",
                description="Modify an existing node in a saved scene by updating its transform, renaming it, reparenting it, or deleting it.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "node_path": {
                            "type": "string",
                            "description": "Scene-relative node path to edit. Use '.' for the root node.",
                            "default": ".",
                        },
                        "transform": {
                            "type": "object",
                            "description": "Optional transform fields to update. Vector values can be passed as objects like {x, y} or {x, y, z}.",
                        },
                        "new_name": {
                            "type": "string",
                            "description": "Optional new name for the target node.",
                        },
                        "new_parent_path": {
                            "type": "string",
                            "description": "Optional new scene-relative parent path for the target node. Use '.' for the root.",
                        },
                        "delete": {
                            "type": "boolean",
                            "description": "Delete the target node. This cannot be combined with other edits.",
                            "default": False,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.edit_scene(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    node_path=args.get("node_path", "."),
                    transform=args.get("transform"),
                    new_name=args.get("new_name"),
                    new_parent_path=args.get("new_parent_path"),
                    delete=bool(args.get("delete", False)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_get_node_properties",
                description="Return the current property list and serialized values for a specific node in a saved scene.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "node_path": {
                            "type": "string",
                            "description": "Scene-relative node path to inspect. Use '.' for the root node.",
                            "default": ".",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.get_node_properties(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    node_path=args.get("node_path", "."),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_get_node_transform",
                description="Read the transform section of a specific node in a saved scene. Currently supports Node2D, Node3D, and Control nodes.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "node_path": {
                            "type": "string",
                            "description": "Scene-relative node path to inspect. Use '.' for the root node.",
                            "default": ".",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.get_node_transform(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    node_path=args.get("node_path", "."),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_update_node_transform",
                description="Update the transform section of a specific node in a saved scene. Currently supports Node2D, Node3D, and Control nodes.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "node_path": {
                            "type": "string",
                            "description": "Scene-relative node path to update. Use '.' for the root node.",
                            "default": ".",
                        },
                        "transform": {
                            "type": "object",
                            "description": "Transform fields to update. Vector values can be passed as objects like {x, y} or {x, y, z}.",
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path", "transform"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.update_node_transform(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    node_path=args.get("node_path", "."),
                    transform=args["transform"],
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_search_docs",
                description="Search the exact local Godot docs for the installed engine version, including class, method, property, signal, and constant docs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Free-text query, such as 'add child node' or 'timer start one shot'.",
                        },
                        "class_name": {
                            "type": "string",
                            "description": "Optional class filter or exact class lookup, such as Node, Timer, Sprite2D, or CharacterBody3D.",
                        },
                        "member_name": {
                            "type": "string",
                            "description": "Optional member filter or exact member lookup, such as add_child, start, position, or ready.",
                        },
                        "member_type": {
                            "type": "string",
                            "description": "Optional result type filter: any, class, method, property, signal, or constant.",
                            "default": "any",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of matches to return.",
                            "default": 8,
                            "minimum": 1,
                        },
                        "refresh_cache": {
                            "type": "boolean",
                            "description": "Whether to rebuild the local docs cache from the selected Godot executable.",
                            "default": False,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.search_docs(
                    query=args.get("query"),
                    class_name=args.get("class_name"),
                    member_name=args.get("member_name"),
                    member_type=args.get("member_type", "any"),
                    max_results=int(args.get("max_results", 8)),
                    refresh_cache=bool(args.get("refresh_cache", False)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_run_scene",
                description="Run a specific Godot scene from an existing project.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Path to the target .tscn file. Absolute, relative, and res:// paths are supported.",
                        },
                        "headless": {
                            "type": "boolean",
                            "description": "Whether to run the scene in headless mode.",
                            "default": False,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path", "scene_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.run_scene(
                    project_path=args["project_path"],
                    scene_path=args["scene_path"],
                    headless=bool(args.get("headless", False)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_run_with_capture",
                description="Run a Godot project or a specific scene for a short capture window and return stdout, stderr, log output, plus parsed warnings and errors.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Optional scene to run instead of the project's configured main scene.",
                        },
                        "headless": {
                            "type": "boolean",
                            "description": "Whether to run in headless mode during capture.",
                            "default": False,
                        },
                        "capture_seconds": {
                            "type": "number",
                            "description": "How long to let the project or scene run before stopping it and collecting output.",
                            "default": 3.0,
                        },
                        "max_output_chars": {
                            "type": "integer",
                            "description": "Maximum number of characters to return for stdout, stderr, and log output excerpts.",
                            "default": 12000,
                            "minimum": 1,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.run_with_capture(
                    project_path=args["project_path"],
                    scene_path=args.get("scene_path"),
                    headless=bool(args.get("headless", False)),
                    capture_seconds=float(args.get("capture_seconds", 3.0)),
                    max_output_chars=int(args.get("max_output_chars", 12000)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_run_with_profiler",
                description=(
                    "Run a Godot project or a specific scene with profiler to report performance such as fps, frame_time_ms, process_time_ms, physics_time_ms, physics_frame_time_ms, navigation_process_ms, memory_static_bytes, object_count, resource_count, node_count, orphan_node_count, physics_2d_active_objects, physics_2d_collision_pairs, physics_2d_island_count, physics_3d_active_objects, physics_3d_collision_pairs, physics_3d_island_count, audio_output_latency_ms."
                    
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": (
                                "Scene to profile (res:// path or absolute path). "
                                "If omitted, the project's configured main scene is used."
                            ),
                        },
                        "duration": {
                            "type": "number",
                            "description": "How many seconds to run the profiler before stopping and collecting results.",
                            "default": 5.0,
                            "minimum": 0.5,
                        },
                        "sample_interval": {
                            "type": "number",
                            "description": (
                                "Minimum interval in seconds between samples. "
                                "0 (default) means sample every frame."
                            ),
                            "default": 0.0,
                            "minimum": 0.0,
                        },
                        "headless": {
                            "type": "boolean",
                            "description": "Whether to run in headless mode. Note: rendering metrics will be zero in headless mode.",
                            "default": False,
                        },
                        "include_samples": {
                            "type": "boolean",
                            "description": "If true, include the raw per-frame sample array in the response (can be large).",
                            "default": False,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.run_profiler(
                    project_path=args["project_path"],
                    scene_path=args.get("scene_path"),
                    duration=float(args.get("duration", 5.0)),
                    sample_interval=float(args.get("sample_interval", 0.0)),
                    headless=bool(args.get("headless", False)),
                    include_samples=bool(args.get("include_samples", False)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_run_with_visual_profiler",
                description=(
                    "Profile a Godot project's rendering/visual performance. "
                    "such as fps, frame_time_ms, render_objects_in_frame, render_primitives_in_frame, render_draw_calls_in_frame, render_video_mem_bytes, navigation_process_ms, object_count, node_count"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": (
                                "Scene to profile (res:// path or absolute path). "
                                "If omitted, the project's configured main scene is used."
                            ),
                        },
                        "duration": {
                            "type": "number",
                            "description": "How many seconds to run the visual profiler before stopping and collecting results.",
                            "default": 5.0,
                            "minimum": 0.5,
                        },
                        "sample_interval": {
                            "type": "number",
                            "description": (
                                "Minimum interval in seconds between samples. "
                                "0 (default) means sample every frame."
                            ),
                            "default": 0.0,
                            "minimum": 0.0,
                        },
                        "headless": {
                            "type": "boolean",
                            "description": (
                                "Whether to run in headless mode. "
                                "WARNING: rendering metrics will be zero in headless mode — "
                                "only use headless if you want non-rendering stats."
                            ),
                            "default": False,
                        },
                        "include_samples": {
                            "type": "boolean",
                            "description": "If true, include the raw per-frame sample array in the response (can be large).",
                            "default": False,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.run_visual_profiler(
                    project_path=args["project_path"],
                    scene_path=args.get("scene_path"),
                    duration=float(args.get("duration", 5.0)),
                    sample_interval=float(args.get("sample_interval", 0.0)),
                    headless=bool(args.get("headless", False)),
                    include_samples=bool(args.get("include_samples", False)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_record_video",
                description=(
                    "Record a video of a Godot project or scene. "
                    "Produces an MP4 if ffmpeg is installed; "
                    "otherwise keeps raw PNG frames and audio. "
                    "Optionally animate the camera along AI-defined waypoints."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Optional scene to record instead of the project's configured main scene.",
                        },
                        "duration": {
                            "type": "number",
                            "description": "How many seconds of video to record.",
                            "default": 5.0,
                            "minimum": 0.5,
                        },
                        "fps": {
                            "type": "integer",
                            "description": "Frames per second for the recording.",
                            "default": 30,
                            "minimum": 1,
                            "maximum": 120,
                        },
                        "resolution": {
                            "type": "object",
                            "description": "Video resolution. Example: {\"width\": 1920, \"height\": 1080}.",
                            "properties": {
                                "width": {"type": "integer", "default": 1920},
                                "height": {"type": "integer", "default": 1080},
                            },
                        },
                        "camera_waypoints": {
                            "type": "array",
                            "description": (
                                "Array of camera waypoints for animated camera movement. "
                                "Each waypoint is an object with: "
                                "position ({x,y,z}), rotation_degrees ({x,y,z}), "
                                "time (seconds into recording), fov (optional). "
                                "The camera smoothly interpolates between waypoints. "
                                "If time is omitted, waypoints are evenly distributed across the duration."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "position": {
                                        "type": "object",
                                        "description": "Camera position {x, y, z}.",
                                    },
                                    "rotation_degrees": {
                                        "type": "object",
                                        "description": "Camera rotation in degrees {x, y, z}.",
                                    },
                                    "time": {
                                        "type": "number",
                                        "description": "Time in seconds when the camera should reach this waypoint.",
                                    },
                                    "fov": {
                                        "type": "number",
                                        "description": "Camera field of view at this waypoint.",
                                    },
                                },
                            },
                        },
                        "camera_node_path": {
                            "type": "string",
                            "description": (
                                "Path to an existing Camera3D node in the scene to animate. "
                                "If omitted, the controller will find the first Camera3D automatically."
                            ),
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.record_video(
                    project_path=args["project_path"],
                    scene_path=args.get("scene_path"),
                    duration=float(args.get("duration", 5.0)),
                    fps=int(args.get("fps", 30)),
                    resolution=args.get("resolution"),
                    camera_waypoints=args.get("camera_waypoints"),
                    camera_node_path=args.get("camera_node_path"),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
            ToolDefinition(
                name="godot_screenshot",
                description="Run a Godot project or specific scene for a short duration, keep the last rendered frame, and return the screenshot path.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to the Godot project directory or its project.godot file.",
                        },
                        "scene_path": {
                            "type": "string",
                            "description": "Optional scene to run instead of the project's configured main scene.",
                        },
                        "capture_seconds": {
                            "type": "number",
                            "description": "How long to let the project run before selecting the last rendered frame as the screenshot.",
                            "default": 2.0,
                        },
                        "fps": {
                            "type": "integer",
                            "description": "Movie-capture framerate used to determine which frame to keep.",
                            "default": 60,
                            "minimum": 1,
                        },
                        "godot_executable": {
                            "type": "string",
                            "description": "Optional explicit path to the Godot executable or .app bundle.",
                        },
                    },
                    "required": ["project_path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.controller.screenshot(
                    project_path=args["project_path"],
                    scene_path=args.get("scene_path"),
                    capture_seconds=float(args.get("capture_seconds", 2.0)),
                    fps=int(args.get("fps", 60)),
                    godot_executable=args.get("godot_executable"),
                ),
            ),
        ]

    def _build_resources(self) -> list[ResourceDefinition]:
        return [
            ResourceDefinition(
                uri="godot://server/tools",
                name="Godot Tool Catalog",
                description="Machine-readable catalog of all Godot MCP tools, descriptions, and input schemas.",
                mime_type="application/json",
                handler=self._render_tool_catalog_resource,
            ),
            ResourceDefinition(
                uri="godot://server/guide",
                name="Godot Server Guide",
                description="High-level usage guide and recommended workflow for this Godot MCP server.",
                mime_type="text/markdown",
                handler=self._render_server_guide_resource,
            ),
        ]

    def _build_resource_templates(self) -> list[ResourceTemplateDefinition]:
        return [
            ResourceTemplateDefinition(
                uri_template="godot://tool/{name}",
                name="Godot Tool Detail",
                description="Inspect one Godot MCP tool by name, including its description and input schema.",
                mime_type="application/json",
            )
        ]

    def _tool_payload(self, tool: ToolDefinition) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }

    def _render_tool_catalog_resource(self) -> str:
        payload = {
            "serverInfo": {
                "name": "godot-mcp",
                "version": __version__,
            },
            "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
            "toolCount": len(self._tools),
            "tools": [self._tool_payload(tool) for tool in self._tools.values()],
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def _render_server_guide_resource(self) -> str:
        tool_lines = "\n".join(f"- `{tool.name}`: {tool.description}" for tool in self._tools.values())
        return "\n".join(
            [
                "# Godot MCP Server Guide",
                "",
                "This server runs over stdio and exposes both tools and MCP resources for self-discovery.",
                "",
                "Useful entry points:",
                "- Read `godot://server/tools` for the full machine-readable tool catalog.",
                "- Read `godot://tool/<tool_name>` for one tool's schema and description.",
                "- Use `tools/list` and `tools/call` for normal MCP tool execution.",
                "",
                "Recommended flow:",
                "1. Detect or provide a Godot executable.",
                "2. Create or open a project.",
                "3. Create a scene and add nodes or primitive meshes.",
                "4. Inspect properties and scene trees while iterating.",
                "5. Validate the scene before running or capturing output.",
                "6. Use `godot_profile` or `godot_visual_profile` to measure performance — do NOT manually write profiler scripts.",
                "",
                "Available tools:",
                tool_lines,
            ]
        )

    def _read_resource(self, uri: str) -> dict[str, Any]:
        if uri in self._resources:
            resource = self._resources[uri]
            return {
                "contents": [
                    {
                        "uri": resource.uri,
                        "mimeType": resource.mime_type,
                        "text": resource.handler(),
                    }
                ]
            }

        parsed = urlparse(uri)
        if parsed.scheme == "godot" and parsed.netloc == "tool":
            tool_name = parsed.path.lstrip("/").strip()
            if not tool_name:
                raise JsonRpcError(-32602, "Resource URI `godot://tool/{name}` requires a tool name.")
            if tool_name not in self._tools:
                raise JsonRpcError(-32602, f"Unknown tool resource `{tool_name}`.")
            tool = self._tools[tool_name]
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(self._tool_payload(tool), indent=2, sort_keys=True),
                    }
                ]
            }

        raise JsonRpcError(-32602, f"Unknown resource `{uri}`.")

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        if not method:
            return None

        if "id" not in message:
            if method == "notifications/initialized":
                self._initialized = True
            return None

        request_id = message["id"]
        params = message.get("params") or {}

        try:
            if method == "initialize":
                client_version = params.get("protocolVersion", SUPPORTED_PROTOCOL_VERSION)
                return self._success(
                    request_id,
                    {
                        "protocolVersion": client_version or SUPPORTED_PROTOCOL_VERSION,
                        "capabilities": {"tools": {}, "resources": {}},
                        "serverInfo": {
                            "name": "godot-mcp",
                            "version": __version__,
                        },
                    },
                )

            if method == "ping":
                return self._success(request_id, {})

            if method == "tools/list":
                return self._success(
                    request_id,
                    {
                        "tools": [
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "inputSchema": tool.input_schema,
                            }
                            for tool in self._tools.values()
                        ]
                    },
                )

            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                if tool_name not in self._tools:
                    raise JsonRpcError(-32602, f"Unknown tool `{tool_name}`.")
                tool = self._tools[tool_name]
                try:
                    result = tool.handler(arguments)
                    return self._success(
                        request_id,
                        {
                            "content": [{"type": "text", "text": format_tool_result(result)}],
                            "structuredContent": result,
                            "isError": False,
                        },
                    )
                except GodotError as exc:
                    return self._success(
                        request_id,
                        {
                            "content": [{"type": "text", "text": str(exc)}],
                            "isError": True,
                        },
                    )

            if method == "resources/list":
                return self._success(
                    request_id,
                    {
                        "resources": [
                            {
                                "uri": resource.uri,
                                "name": resource.name,
                                "description": resource.description,
                                "mimeType": resource.mime_type,
                            }
                            for resource in self._resources.values()
                        ]
                    },
                )

            if method == "resources/templates/list":
                return self._success(
                    request_id,
                    {
                        "resourceTemplates": [
                            {
                                "uriTemplate": template.uri_template,
                                "name": template.name,
                                "description": template.description,
                                "mimeType": template.mime_type,
                            }
                            for template in self._resource_templates
                        ]
                    },
                )

            if method == "resources/read":
                uri = str(params.get("uri", "")).strip()
                if not uri:
                    raise JsonRpcError(-32602, "`resources/read` requires a `uri`.")
                return self._success(request_id, self._read_resource(uri))

            if method == "prompts/list":
                return self._success(request_id, {"prompts": []})

            raise JsonRpcError(-32601, f"Method `{method}` is not supported.")
        except JsonRpcError as exc:
            return self._error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - last-resort protocol protection
            return self._error(request_id, -32603, f"Internal error: {exc}")

    @staticmethod
    def _success(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    def serve(self) -> None:
        while True:
            message = read_message()
            if message is None:
                break
            response = self.handle_message(message)
            if response is not None:
                write_message(response)


def main() -> None:
    GodotMcpServer().serve()
