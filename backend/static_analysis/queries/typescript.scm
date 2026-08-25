; TypeScript / TSX 的符号与 import 提取。
;
; 节点结构用 probe 实测确认（backend/static_analysis/probe.py），未凭猜。
;
; **这份 query 不能给 javascript grammar 用。** 实测结论：`interface_declaration`、
; `type_alias_declaration`、`type_identifier` 在 javascript grammar 里不存在，
; `ts.Query()` 构造时直接抛 QueryError（不是静默返回空结果）。JS 文件按计划的语言
; 范围只计入文件树，故 parser.py 的 SUFFIX_TO_GRAMMAR 不再收录 .js/.mjs/.cjs/.jsx，
; 由「未支持的扩展名」路径显式标注——而不是让 query 编译崩溃后落进兜底分支，那样
; 未解析原因会写成「解析失败」，掩盖真实语言范围。
; TSX 与 TypeScript 共用这一份：TSX 是 TS 的超集，实测两者对上述节点均编译通过。
;
; 与 Python 最大的结构差异：**export 是包裹层，不是修饰符。** 实测确认：
;
;   export function named() {}   -> export_statement > function_declaration > identifier
;   export class Cls {}          -> export_statement > class_declaration > type_identifier
;   export const fn = () => {}   -> export_statement > lexical_declaration
;                                     > variable_declarator > identifier
;   export * from './barrel'     -> export_statement（无 declaration，有 source 字段）
;
; 所以「捕获所有 identifier」会连带捕获参数名、变量名、属性名——噪声远超信号。
; 这里的对策是按声明类型写精确路径，让 query 层就把噪声挡掉：名字节点的类型本身
; 就是过滤器（类与接口用 type_identifier，方法用 property_identifier，函数用
; identifier），比在 extract.py 里按父节点类型反查更声明式，也更好调。

; ── 符号定义 ──────────────────────────────────────────────────────────
; 具名函数。export 包裹与否都命中——匹配的是内层 declaration，不是 export_statement。
(function_declaration name: (identifier) @def_function)
(generator_function_declaration name: (identifier) @def_function)

; 类。注意名字节点是 type_identifier 而非 identifier。
(class_declaration name: (type_identifier) @def_class)
(abstract_class_declaration name: (type_identifier) @def_class)

; 类内方法。TS 语法天然区分方法与函数（method_definition vs function_declaration），
; 无需像 Python 那样查父节点链。
(method_definition name: (property_identifier) @def_method)

(interface_declaration name: (type_identifier) @def_interface)
(type_alias_declaration name: (type_identifier) @def_type_alias)

; `const fn = () => {}` 形态。value 的类型是关键约束：只有箭头函数或函数表达式才算
; 符号定义，否则 `const config = {...}` 也会被当成函数进符号表。
(variable_declarator
  name: (identifier) @def_function
  value: [(arrow_function) (function_expression)])

; 类属性上的箭头函数：`class C { handle = () => {} }`。React 组件里很常见，
; 结构是 public_field_definition 而非 variable_declarator，需单独一条。
(public_field_definition
  name: (property_identifier) @def_method
  value: [(arrow_function) (function_expression)])

; ── import ────────────────────────────────────────────────────────────
; 整条语句捕获，理由同 python.scm：capture 结果扁平，字段级捕获会丢掉名字与
; 语句的归属关系。import 子句的形态（具名/默认/命名空间/type-only）由 extract.py
; 按 import_clause 的子节点类型区分。
(import_statement) @import_statement

; 再导出。`export * from './b'` 与 `export { a } from './b'` 都有 source 字段；
; 无 source 的 export（`export const x = 1`）不是依赖边，由 extract.py 按 source
; 是否存在过滤，故这里统一捕获 export_statement。
(export_statement) @reexport_statement
