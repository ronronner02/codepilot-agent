; Python 的符号与 import 提取。
;
; 节点结构用 probe 实测确认（backend/static_analysis/probe.py），未凭猜。
;
; 三个决定，按影响面从大到小：
;
; 1. **只捕获名字节点，不捕获整个定义节点。** 两种写法都能拿到定义：
;    `(function_definition) @def`，或 `(function_definition name: (identifier) @name)`。
;    选后者，因为同一节点被多个 capture 命中会产出重复 Symbol，而名字节点到定义
;    节点只需 `node.parent` 一步；反过来（定义节点找名字）还得再查一次字段。
;    捕获点越少，extract.py 的去重负担越小。
;
; 2. **不单独捕获 decorated_definition。** 它包裹 function_definition，直接匹配内层
;    也能命中被装饰的定义。行范围从装饰器算起还是从 `def` 算起，是一个需要跨语言
;    一致的决定（TS 那边对应 export 包裹层），统一放在 extract.py 的 _definition_node
;    里表达，query 层不重复一遍。
;
; 3. **import 捕获整条语句，不捕获内部字段。** capture 结果是扁平的
;    `dict[capture_name, list[Node]]`，字段级 capture 会丢掉「哪个名字属于哪条
;    import」的归属关系——`from a import x` 与 `from b import y` 的四个 capture 混进
;    两个列表后无法配对。所以这里只标出语句边界，内部结构由 extract.py 按字段名走。

; ── 符号定义 ──────────────────────────────────────────────────────────
; Python 语法不区分函数与方法，两者同为 function_definition。METHOD 的判定靠
; extract.py 往上找 class_definition，这条父节点链是唯一依据。
(function_definition name: (identifier) @def_function)
(class_definition name: (identifier) @def_class)

; ── import ────────────────────────────────────────────────────────────
; import os / import os.path as p（一条语句可含多个目标：import os, sys）
(import_statement) @import_module

; from x import y / from . import y / from ..pkg.deep import y
; 相对导入的点数在 module_name 的 relative_import 节点文本里，丢了 U4 就无法
; 归一化到实际目录。
(import_from_statement) @import_from
