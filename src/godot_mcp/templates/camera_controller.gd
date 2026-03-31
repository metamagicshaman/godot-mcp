extends Node
## Godot MCP Camera Controller — injected as autoload to animate a camera along waypoints.
## Reads waypoints JSON from --waypoints-path.

var _waypoints: Array = []
var _duration: float = 5.0
var _elapsed: float = 0.0
var _camera: Camera3D = null
var _camera_node_path: String = ""
var _total_waypoint_time: float = 0.0
var _started: bool = false


func _ready() -> void:
	var args := _parse_args(OS.get_cmdline_user_args())
	_duration = float(args.get("camera-duration", "5.0"))
	_camera_node_path = str(args.get("camera-node-path", ""))
	var waypoints_path := str(args.get("waypoints-path", ""))

	if not waypoints_path.is_empty():
		var file := FileAccess.open(waypoints_path, FileAccess.READ)
		if file:
			var parsed: Variant = JSON.parse_string(file.get_as_text())
			if parsed is Array:
				_waypoints = parsed

	# Calculate total waypoint time.
	for wp in _waypoints:
		if wp is Dictionary and wp.has("time"):
			var t: float = float(wp["time"])
			if t > _total_waypoint_time:
				_total_waypoint_time = t

	if _total_waypoint_time <= 0.0 and _waypoints.size() > 0:
		_total_waypoint_time = _duration
		var count: int = _waypoints.size()
		for i in range(count):
			if _waypoints[i] is Dictionary:
				_waypoints[i]["time"] = (_duration * float(i)) / max(float(count - 1), 1.0)

	process_priority = -1000  # Run before everything else to position camera early.


func _process(delta: float) -> void:
	if not _started:
		_started = true
		_find_camera()
		if _camera != null and _waypoints.size() > 0:
			_apply_waypoint_at_time(0.0)
		return

	_elapsed += delta

	if _camera != null and _waypoints.size() > 0:
		_apply_waypoint_at_time(_elapsed)


func _find_camera() -> void:
	if not _camera_node_path.is_empty():
		var found := get_tree().root.get_node_or_null(_camera_node_path)
		if found is Camera3D:
			_camera = found
			_camera.current = true
			return

	# Search for any Camera3D in the scene.
	_camera = _find_camera_recursive(get_tree().root)
	if _camera != null:
		_camera.current = true


func _find_camera_recursive(node: Node) -> Camera3D:
	if node is Camera3D:
		return node
	for child in node.get_children():
		var found := _find_camera_recursive(child)
		if found != null:
			return found
	return null


func _apply_waypoint_at_time(t: float) -> void:
	if _waypoints.size() == 0 or _camera == null:
		return

	# Clamp to waypoint time range.
	var clamped_t: float = clampf(t, 0.0, _total_waypoint_time)

	# Find the two surrounding waypoints.
	var prev_wp: Dictionary = _waypoints[0]
	var next_wp: Dictionary = _waypoints[0]

	for i in range(_waypoints.size()):
		var wp: Dictionary = _waypoints[i]
		var wp_time: float = float(wp.get("time", 0.0))
		if wp_time <= clamped_t:
			prev_wp = wp
			if i + 1 < _waypoints.size():
				next_wp = _waypoints[i + 1]
			else:
				next_wp = wp
		else:
			next_wp = wp
			break

	var prev_time: float = float(prev_wp.get("time", 0.0))
	var next_time: float = float(next_wp.get("time", 0.0))

	var weight: float = 0.0
	if next_time > prev_time:
		weight = clampf((clamped_t - prev_time) / (next_time - prev_time), 0.0, 1.0)
		# Apply easing for smooth motion.
		weight = _smoothstep(weight)

	var prev_pos := _dict_to_vector3(prev_wp.get("position", {}))
	var next_pos := _dict_to_vector3(next_wp.get("position", {}))
	_camera.position = prev_pos.lerp(next_pos, weight)

	var prev_rot := _dict_to_vector3(prev_wp.get("rotation_degrees", {}))
	var next_rot := _dict_to_vector3(next_wp.get("rotation_degrees", {}))
	_camera.rotation_degrees = prev_rot.lerp(next_rot, weight)

	# Interpolate FOV if provided.
	if prev_wp.has("fov") or next_wp.has("fov"):
		var prev_fov: float = float(prev_wp.get("fov", _camera.fov))
		var next_fov: float = float(next_wp.get("fov", _camera.fov))
		_camera.fov = lerpf(prev_fov, next_fov, weight)


func _smoothstep(t: float) -> float:
	return t * t * (3.0 - 2.0 * t)


func _dict_to_vector3(d: Variant) -> Vector3:
	if d is Dictionary:
		return Vector3(
			float(d.get("x", 0.0)),
			float(d.get("y", 0.0)),
			float(d.get("z", 0.0))
		)
	if d is Array and d.size() == 3:
		return Vector3(float(d[0]), float(d[1]), float(d[2]))
	return Vector3.ZERO


func _parse_args(raw_args: PackedStringArray) -> Dictionary:
	var parsed := {}
	var i := 0
	while i < raw_args.size():
		var arg: String = raw_args[i]
		if arg.begins_with("--") and i + 1 < raw_args.size():
			parsed[arg.substr(2)] = raw_args[i + 1]
			i += 2
		else:
			i += 1
	return parsed
