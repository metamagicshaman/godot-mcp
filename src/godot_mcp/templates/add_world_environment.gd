extends SceneTree


func _init() -> void:
    var args := _parse_args(OS.get_cmdline_user_args())
    var scene_path := str(args.get("scene-path", "")).strip_edges()
    var parent_path := str(args.get("parent-path", ".")).strip_edges()
    var node_name := str(args.get("node-name", "WorldEnvironment")).strip_edges()
    var config_path := str(args.get("config-path", "")).strip_edges()

    if scene_path.is_empty():
        printerr("Missing required argument: --scene-path")
        quit(1)
        return
    if node_name.is_empty():
        printerr("Missing required argument: --node-name")
        quit(1)
        return
    if config_path.is_empty():
        printerr("Missing required argument: --config-path")
        quit(1)
        return

    var root := _load_scene_root(scene_path)
    if root == null:
        return

    var parent_node := _resolve_parent_node(root, parent_path)
    if parent_node == null:
        return

    for child in parent_node.get_children():
        if child is Node and str(child.name) == node_name:
            printerr("A child named %s already exists under %s." % [node_name, _scene_relative_path(root, parent_node)])
            quit(1)
            return

    var config := _read_config(config_path)
    if config == null:
        return

    var raw_environment_parameters: Variant = config.get("environment_parameters", {})
    var raw_node_parameters: Variant = config.get("node_parameters", {})
    if not (raw_environment_parameters is Dictionary):
        printerr("`environment_parameters` must be a JSON object.")
        quit(1)
        return
    if not (raw_node_parameters is Dictionary):
        printerr("`node_parameters` must be a JSON object.")
        quit(1)
        return
    var environment_parameters: Dictionary = raw_environment_parameters
    var node_parameters: Dictionary = raw_node_parameters

    var world_environment := WorldEnvironment.new()
    world_environment.name = node_name
    world_environment.environment = Environment.new()

    var supported_node_parameters := _supported_parameters(world_environment, ["environment"])
    var supported_environment_parameters := _supported_parameters(world_environment.environment, [])

    var updated_node_parameters := _apply_property_updates(
        world_environment,
        node_parameters,
        supported_node_parameters,
        "node_parameters"
    )
    var updated_environment_parameters := _apply_property_updates(
        world_environment.environment,
        environment_parameters,
        supported_environment_parameters,
        "environment_parameters"
    )

    parent_node.add_child(world_environment)
    if world_environment != root:
        world_environment.owner = root

    var save_result := _save_scene(scene_path, root)
    if not save_result:
        return

    print(JSON.stringify({
        "scene_path": scene_path,
        "parent_path": _scene_relative_path(root, parent_node),
        "node_path": _scene_relative_path(root, world_environment),
        "node_name": node_name,
        "node_type": world_environment.get_class(),
        "environment_created": world_environment.environment != null,
        "node_parameters": _serialize_selected_properties(world_environment, updated_node_parameters),
        "environment_parameters": _serialize_selected_properties(world_environment.environment, updated_environment_parameters),
        "updated_node_parameters": updated_node_parameters,
        "updated_environment_parameters": updated_environment_parameters,
        "supported_node_parameters": supported_node_parameters,
        "supported_environment_parameters": supported_environment_parameters,
    }))
    quit()


func _load_scene_root(scene_path: String) -> Node:
    var resource := ResourceLoader.load(scene_path)
    if resource == null or not (resource is PackedScene):
        printerr("Could not load PackedScene: %s" % scene_path)
        quit(1)
        return null

    var packed_scene: PackedScene = resource
    var root := packed_scene.instantiate()
    if root == null or not (root is Node):
        printerr("PackedScene.instantiate did not return a Node root.")
        quit(1)
        return null
    return root


func _resolve_parent_node(root: Node, parent_path: String) -> Node:
    if parent_path.is_empty() or parent_path == ".":
        return root

    var found := root.get_node_or_null(parent_path)
    if found == null or not (found is Node):
        printerr("Parent node was not found at path: %s" % parent_path)
        quit(1)
        return null
    return found


func _read_config(config_path: String) -> Dictionary:
    var file := FileAccess.open(config_path, FileAccess.READ)
    if file == null:
        printerr("Could not open config file: %s" % config_path)
        quit(1)
        return {}

    var raw_config: Variant = JSON.parse_string(file.get_as_text())
    if raw_config == null or not (raw_config is Dictionary):
        printerr("WorldEnvironment config file must contain a JSON object.")
        quit(1)
        return {}
    return raw_config


func _save_scene(scene_path: String, root: Node) -> bool:
    var repacked := PackedScene.new()
    var pack_error := repacked.pack(root)
    if pack_error != OK:
        printerr("PackedScene.pack failed with code %s" % pack_error)
        quit(1)
        return false

    var save_error := ResourceSaver.save(repacked, scene_path)
    if save_error != OK:
        printerr("ResourceSaver.save failed with code %s" % save_error)
        quit(1)
        return false
    return true


func _supported_parameters(target: Object, excluded_names: Array) -> Array:
    var excluded := {}
    for raw_name in excluded_names:
        excluded[str(raw_name)] = true

    var supported := []
    for raw_entry in target.get_property_list():
        if not (raw_entry is Dictionary):
            continue

        var entry: Dictionary = raw_entry
        var property_name := str(entry.get("name", "")).strip_edges()
        if property_name.is_empty() or excluded.has(property_name):
            continue

        var usage := int(entry.get("usage", 0))
        if usage & (PROPERTY_USAGE_GROUP | PROPERTY_USAGE_CATEGORY | PROPERTY_USAGE_SUBGROUP):
            continue
        if (usage & PROPERTY_USAGE_STORAGE) == 0:
            continue

        var type_id := int(entry.get("type", TYPE_NIL))
        supported.append({
            "name": property_name,
            "class_name": str(entry.get("class_name", "")),
            "type": type_id,
            "type_name": type_string(type_id),
            "hint": int(entry.get("hint", 0)),
            "hint_string": str(entry.get("hint_string", "")),
            "usage": usage,
            "settable_from_json": _can_set_from_json(type_id),
        })

    supported.sort_custom(func(a, b): return str(a.get("name", "")) < str(b.get("name", "")))
    return supported


func _can_set_from_json(type_id: int) -> bool:
    return type_id in [
        TYPE_NIL,
        TYPE_BOOL,
        TYPE_INT,
        TYPE_FLOAT,
        TYPE_STRING,
        TYPE_STRING_NAME,
        TYPE_NODE_PATH,
        TYPE_VECTOR2,
        TYPE_VECTOR2I,
        TYPE_VECTOR3,
        TYPE_VECTOR3I,
        TYPE_VECTOR4,
        TYPE_VECTOR4I,
        TYPE_COLOR,
        TYPE_RECT2,
        TYPE_RECT2I,
        TYPE_ARRAY,
        TYPE_DICTIONARY,
        TYPE_OBJECT,
    ]


func _apply_property_updates(target: Object, updates: Dictionary, supported: Array, scope_name: String) -> Array:
    if updates.is_empty():
        return []

    var property_map := _property_map_from_supported(supported)
    var updated := []
    for raw_key in updates.keys():
        var property_name := str(raw_key)
        if not property_map.has(property_name):
            printerr("Unsupported %s field: %s" % [scope_name, property_name])
            quit(1)
            return []

        var property_info: Dictionary = property_map[property_name]
        var expected_type := int(property_info.get("type", TYPE_NIL))
        var current_value := target.get(property_name)
        var field_name := "%s.%s" % [scope_name, property_name]
        var coerced_value := _coerce_property_value(updates[property_name], expected_type, current_value, field_name)
        target.set(property_name, coerced_value)
        updated.append(property_name)

    return updated


func _property_map_from_supported(supported: Array) -> Dictionary:
    var by_name := {}
    for raw_entry in supported:
        if not (raw_entry is Dictionary):
            continue
        var entry: Dictionary = raw_entry
        var property_name := str(entry.get("name", ""))
        if property_name.is_empty():
            continue
        by_name[property_name] = entry
    return by_name


func _coerce_property_value(raw_value: Variant, expected_type: int, current_value: Variant, field_name: String) -> Variant:
    match expected_type:
        TYPE_NIL:
            return raw_value
        TYPE_BOOL:
            return bool(raw_value)
        TYPE_INT:
            return int(raw_value)
        TYPE_FLOAT:
            return float(raw_value)
        TYPE_STRING:
            return str(raw_value)
        TYPE_STRING_NAME:
            return StringName(str(raw_value))
        TYPE_NODE_PATH:
            return NodePath(str(raw_value))
        TYPE_VECTOR2:
            var current_vector2 := current_value if current_value is Vector2 else Vector2.ZERO
            return _merge_vector2(current_vector2, raw_value, field_name)
        TYPE_VECTOR2I:
            var current_vector2i := current_value if current_value is Vector2i else Vector2i.ZERO
            var merged_vector2 := _merge_vector2(Vector2(current_vector2i), raw_value, field_name)
            return Vector2i(int(round(merged_vector2.x)), int(round(merged_vector2.y)))
        TYPE_VECTOR3:
            var current_vector3 := current_value if current_value is Vector3 else Vector3.ZERO
            return _merge_vector3(current_vector3, raw_value, field_name)
        TYPE_VECTOR3I:
            var current_vector3i := current_value if current_value is Vector3i else Vector3i.ZERO
            var merged_vector3 := _merge_vector3(Vector3(current_vector3i), raw_value, field_name)
            return Vector3i(int(round(merged_vector3.x)), int(round(merged_vector3.y)), int(round(merged_vector3.z)))
        TYPE_VECTOR4:
            var current_vector4 := current_value if current_value is Vector4 else Vector4.ZERO
            return _merge_vector4(current_vector4, raw_value, field_name)
        TYPE_VECTOR4I:
            var current_vector4i := current_value if current_value is Vector4i else Vector4i.ZERO
            var merged_vector4 := _merge_vector4(Vector4(current_vector4i), raw_value, field_name)
            return Vector4i(
                int(round(merged_vector4.x)),
                int(round(merged_vector4.y)),
                int(round(merged_vector4.z)),
                int(round(merged_vector4.w))
            )
        TYPE_COLOR:
            var current_color := current_value if current_value is Color else Color.WHITE
            return _merge_color(current_color, raw_value, field_name)
        TYPE_RECT2:
            var current_rect2 := current_value if current_value is Rect2 else Rect2()
            return _merge_rect2(current_rect2, raw_value, field_name)
        TYPE_RECT2I:
            var current_rect2i := current_value if current_value is Rect2i else Rect2i()
            var merged_rect2 := _merge_rect2(Rect2(current_rect2i), raw_value, field_name)
            return Rect2i(
                int(round(merged_rect2.position.x)),
                int(round(merged_rect2.position.y)),
                int(round(merged_rect2.size.x)),
                int(round(merged_rect2.size.y))
            )
        TYPE_ARRAY:
            if raw_value is Array:
                return raw_value
            _invalid_property_type(field_name, "Array")
            return current_value
        TYPE_DICTIONARY:
            if raw_value is Dictionary:
                return raw_value
            _invalid_property_type(field_name, "Dictionary")
            return current_value
        TYPE_OBJECT:
            return _coerce_object_value(raw_value, current_value, field_name)
        _:
            return raw_value


func _coerce_object_value(raw_value: Variant, current_value: Variant, field_name: String) -> Variant:
    if raw_value == null:
        return null

    var resource_path := ""
    if raw_value is String:
        resource_path = str(raw_value).strip_edges()
    elif raw_value is Dictionary:
        var payload: Dictionary = raw_value
        if payload.has("resource_path"):
            resource_path = str(payload.get("resource_path", "")).strip_edges()
        elif payload.has("__resource_path"):
            resource_path = str(payload.get("__resource_path", "")).strip_edges()

    if resource_path.is_empty():
        printerr(
            "Object field %s must be null, a resource path string, or an object containing `resource_path`."
            % field_name
        )
        quit(1)
        return current_value

    var loaded := ResourceLoader.load(resource_path)
    if loaded == null:
        printerr("Could not load resource for %s: %s" % [field_name, resource_path])
        quit(1)
        return current_value
    return loaded


func _invalid_property_type(field_name: String, expected_description: String) -> void:
    printerr("Field %s must be %s." % [field_name, expected_description])
    quit(1)


func _merge_vector2(current: Vector2, update: Variant, field_name: String) -> Vector2:
    if update is Array:
        var values: Array = update
        if values.size() != 2:
            printerr("Field %s must contain exactly 2 values." % field_name)
            quit(1)
            return current
        return Vector2(float(values[0]), float(values[1]))

    if update is Dictionary:
        var values: Dictionary = update
        return Vector2(float(values.get("x", current.x)), float(values.get("y", current.y)))

    printerr("Field %s must be a dictionary or a 2-item array." % field_name)
    quit(1)
    return current


func _merge_vector3(current: Vector3, update: Variant, field_name: String) -> Vector3:
    if update is Array:
        var values: Array = update
        if values.size() != 3:
            printerr("Field %s must contain exactly 3 values." % field_name)
            quit(1)
            return current
        return Vector3(float(values[0]), float(values[1]), float(values[2]))

    if update is Dictionary:
        var values: Dictionary = update
        return Vector3(float(values.get("x", current.x)), float(values.get("y", current.y)), float(values.get("z", current.z)))

    printerr("Field %s must be a dictionary or a 3-item array." % field_name)
    quit(1)
    return current


func _merge_vector4(current: Vector4, update: Variant, field_name: String) -> Vector4:
    if update is Array:
        var values: Array = update
        if values.size() != 4:
            printerr("Field %s must contain exactly 4 values." % field_name)
            quit(1)
            return current
        return Vector4(float(values[0]), float(values[1]), float(values[2]), float(values[3]))

    if update is Dictionary:
        var values: Dictionary = update
        return Vector4(
            float(values.get("x", current.x)),
            float(values.get("y", current.y)),
            float(values.get("z", current.z)),
            float(values.get("w", current.w))
        )

    printerr("Field %s must be a dictionary or a 4-item array." % field_name)
    quit(1)
    return current


func _merge_color(current: Color, update: Variant, field_name: String) -> Color:
    if update is String:
        return Color.from_string(update, current)

    if update is Array:
        var values: Array = update
        if values.size() != 3 and values.size() != 4:
            printerr("Field %s must contain 3 or 4 values." % field_name)
            quit(1)
            return current
        return Color(
            float(values[0]),
            float(values[1]),
            float(values[2]),
            float(values[3]) if values.size() == 4 else current.a
        )

    if update is Dictionary:
        var values: Dictionary = update
        return Color(
            float(values.get("r", current.r)),
            float(values.get("g", current.g)),
            float(values.get("b", current.b)),
            float(values.get("a", current.a))
        )

    printerr("Field %s must be a color string, object, or array." % field_name)
    quit(1)
    return current


func _merge_rect2(current: Rect2, update: Variant, field_name: String) -> Rect2:
    if not (update is Dictionary):
        printerr("Field %s must be a dictionary with `position` and/or `size`." % field_name)
        quit(1)
        return current

    var values: Dictionary = update
    var position := current.position
    var size := current.size
    if values.has("position"):
        position = _merge_vector2(position, values["position"], "%s.position" % field_name)
    if values.has("size"):
        size = _merge_vector2(size, values["size"], "%s.size" % field_name)
    return Rect2(position, size)


func _serialize_selected_properties(target: Object, property_names: Array) -> Dictionary:
    var serialized := {}
    for raw_name in property_names:
        var property_name := str(raw_name)
        serialized[property_name] = _serialize_variant(target.get(property_name))
    return serialized


func _serialize_variant(value: Variant) -> Variant:
    var value_type := typeof(value)
    match value_type:
        TYPE_NIL, TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING:
            return value
        TYPE_STRING_NAME, TYPE_NODE_PATH:
            return str(value)
        TYPE_VECTOR2, TYPE_VECTOR2I:
            return {"x": value.x, "y": value.y}
        TYPE_VECTOR3, TYPE_VECTOR3I:
            return {"x": value.x, "y": value.y, "z": value.z}
        TYPE_VECTOR4, TYPE_VECTOR4I:
            return {"x": value.x, "y": value.y, "z": value.z, "w": value.w}
        TYPE_COLOR:
            return {"r": value.r, "g": value.g, "b": value.b, "a": value.a}
        TYPE_RECT2, TYPE_RECT2I:
            return {
                "position": _serialize_variant(value.position),
                "size": _serialize_variant(value.size),
            }
        TYPE_DICTIONARY:
            var mapped := {}
            var dictionary: Dictionary = value
            for raw_key in dictionary.keys():
                mapped[str(raw_key)] = _serialize_variant(dictionary[raw_key])
            return mapped
        TYPE_ARRAY:
            var items := []
            var array_value: Array = value
            for item in array_value:
                items.append(_serialize_variant(item))
            return items
        TYPE_OBJECT:
            if value == null:
                return null
            if value is Resource:
                var resource: Resource = value
                return {
                    "__type": "Resource",
                    "class_name": resource.get_class(),
                    "resource_path": resource.resource_path,
                }
            var object_value: Object = value
            return {
                "__type": "Object",
                "class_name": object_value.get_class(),
                "instance_id": int(object_value.get_instance_id()),
            }
        _:
            return {
                "__type": type_string(value_type),
                "value": var_to_str(value),
            }


func _scene_relative_path(root: Node, node: Node) -> String:
    if node == root:
        return "."
    return str(root.get_path_to(node))


func _parse_args(argv: PackedStringArray) -> Dictionary:
    var parsed := {}
    var index := 0
    while index < argv.size():
        var key := argv[index]
        if not key.begins_with("--"):
            index += 1
            continue

        var name := key.substr(2)
        var value := "true"
        if index + 1 < argv.size() and not argv[index + 1].begins_with("--"):
            value = argv[index + 1]
            index += 1

        parsed[name] = value
        index += 1

    return parsed
