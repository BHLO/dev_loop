"""符号索引与静态分析盲区检测。

两个职责：

1. **给出准确的符号表**——Python 走 ast（函数、方法、类、导入、装饰器路由），
   Java / TS 走「块切分 + 声明头正则」（切成声明段再匹配，不受换行与缩进影响），
   Go 及其余语言退回逐行正则。比纯逐行正则在 Python/Java/TS 上准得多。
2. **明确说出「看不见什么」**——反射、DI 容器、动态导入、字符串拼装的路由、
   注册表模式、``**kwargs`` 转发。这些位置静态分析必然漏，与其假装准确，
   不如把它们逐条列出来交给人工判断。

用法（由 devloop.py 调用，不单独运行）::

    from astindex import index_file, py_defs
    info = index_file(Path("src/api/user.py"))
    info["defs"]        # [{"name": "get_user", "kind": "function", "line": 12}]
    info["blind"]       # [{"category": "反射", "line": 30, "code": ..., "why": ..., "action": ...}]
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

MAX_READ = 1_200_000

PY_EXTS = {".py", ".pyi"}
JS_EXTS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
JVM_EXTS = {".java", ".kt", ".kts", ".scala", ".groovy"}
GO_EXTS = {".go"}
OTHER_EXTS = {".rs", ".cs", ".c", ".h", ".cpp", ".cc", ".hpp", ".php", ".rb",
              ".swift", ".m", ".dart", ".lua", ".ex", ".exs"}

# 这些装饰器是框架的路由声明，算「符号」不算「盲区」
ROUTE_DECORATORS = {
    "route", "get", "post", "put", "delete", "patch", "head", "options",
    "websocket", "api_route", "middleware", "exception_handler",
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping", "DeleteMapping",
    "PatchMapping", "Path", "GET", "POST", "PUT", "DELETE", "PATCH",
}

# 不改变语义的常见装饰器，不参与「装饰器注册」判断
BENIGN_DECORATORS = {
    "staticmethod", "classmethod", "property", "abstractmethod", "override",
    "dataclass", "functools.wraps", "wraps", "lru_cache", "cache", "cached_property",
    "pytest.fixture", "fixture", "deprecated", "total_ordering", "singledispatch",
    "Injectable", "Component", "Service", "Repository", "Controller", "Configuration",
}


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_READ:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _line_of(text: str, lineno: int) -> str:
    lines = text.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:160]
    return ""


# --------------------------------------------------------------------------- Python

def py_defs(tree: ast.AST) -> list[dict]:
    """函数、方法、类。方法名记成 ``Class.method``，便于精确引用匹配。"""
    out: list[dict] = []

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                kind = "method" if prefix else "function"
                out.append({"name": name, "short": child.name, "kind": kind,
                            "line": child.lineno,
                            "decorators": [_dec_name(d) for d in child.decorator_list]})
                visit(child, prefix=f"{name}.")
            elif isinstance(child, ast.ClassDef):
                out.append({"name": f"{prefix}{child.name}", "short": child.name,
                            "kind": "class", "line": child.lineno,
                            "bases": [_dec_name(b) for b in child.bases]})
                visit(child, prefix=f"{prefix}{child.name}.")
            else:
                visit(child, prefix=prefix)

    visit(tree)
    return out


def _dec_name(node: ast.AST) -> str:
    """装饰器/基类的可读名：``app.route`` / ``Depends`` / ``BaseModel``。"""
    if isinstance(node, ast.Call):
        return _dec_name(node.func)
    if isinstance(node, ast.Attribute):
        base = _dec_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _dec_name(node.value)
    return ""


def py_imports(tree: ast.AST) -> list[dict]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append({"module": alias.name, "name": "", "alias": alias.asname or "",
                            "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            mod = "." * (node.level or 0) + (node.module or "")
            for alias in node.names:
                out.append({"module": mod, "name": alias.name,
                            "alias": alias.asname or "", "line": node.lineno})
    return out


def _const_str(node: ast.AST) -> str | None:
    """能静态确定成字符串就返回，否则 None（含 f-string 拼接的情况）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _looks_like_path(literal: str) -> bool:
    """这个字面量像不像 URL / 路由的一部分。

    以 / 开头（/api/users）、含协议头（https://…），或带常见接口前缀。
    `Path / name` 的除号、日志里的 "读/写" 都不该命中。
    """
    s = literal.strip()
    # 只有斜杠、点、横线的不算：Path 拼接里的 "/"、正则里的转义都会走到这里
    if not s or not s.strip("/.- "):
        return False
    if "://" in s:
        return True
    # 斜杠后面必须紧跟路径成分，否则 " / P1 " 这种也会被误判
    if re.match(r"^/[A-Za-z0-9_\-{<:.$]", s):
        return True
    return bool(re.search(r"/(?:api|v\d|admin|internal|public|assets|static)/", s))


def py_routes(tree: ast.AST, text: str) -> tuple[list[dict], list[dict]]:
    """装饰器声明的路由。返回 (literal_routes, dynamic_routes)。"""
    literal, dynamic = [], []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            name = _dec_name(target)
            short = name.rsplit(".", 1)[-1]
            if short not in ROUTE_DECORATORS:
                continue
            if not isinstance(dec, ast.Call) or not dec.args:
                continue
            path = _const_str(dec.args[0])
            item = {"handler": node.name, "line": dec.lineno, "decorator": name}
            if path is None:
                item["expr"] = _line_of(text, dec.lineno)
                dynamic.append(item)
            else:
                item["path"] = path
                item["method"] = short.upper()
                literal.append(item)
    return literal, dynamic


PY_CATEGORY = {
    "getattr": ("反射", "按字符串名取属性，静态分析不知道取到的是哪个符号",
                "人工确认目标符号；若目标在本仓库，补进改造清单"),
    "setattr": ("反射", "按字符串名写属性，改字段名时这里不会报错",
                "确认字段是否受本次改动影响"),
    "globals": ("反射", "遍历模块全局符号，新增/改名函数可能被这里间接引用",
                "确认是否按名字查函数或类"),
    "locals": ("反射", "从局部符号表取名字，调用关系不可静态追踪",
                "人工确认取值来源"),
    "vars": ("反射", "取对象 __dict__，字段改名不会报错",
                "确认字段是否受本次改动影响"),
    "import_module": ("动态导入", "运行时按字符串导入模块，模块改名/移动不会被发现",
                      "grep 目标模块名，确认是否指向本次改动"),
    "__import__": ("动态导入", "运行时按字符串导入，静态不可见",
                   "确认导入目标是否被改动"),
    "eval": ("动态执行", "执行字符串代码，无法静态分析",
             "确认输入来源与拼装方式"),
    "exec": ("动态执行", "执行字符串代码，无法静态分析",
             "确认输入来源与拼装方式"),
    "subclasses": ("注册表", "遍历子类做插件发现，新增子类不经过显式引用",
                   "确认新增类是否需要在此处登记"),
    "register": ("注册表", "注册到注册表，名字与实现是运行期绑定",
                 "确认注册名是否需要同步修改"),
    "register_blueprint": ("动态路由", "蓝图挂载点，路由表在运行期拼装",
                           "确认蓝图前缀与本次改动是否相关"),
    "include_router": ("动态路由", "路由在运行期拼装，前缀可能与声明处不同",
                       "确认前缀与路径是否受本次改动影响"),
    "Depends": ("DI 容器", "依赖注入，参数由容器在运行期解析",
                "确认被注入的依赖是否在改造清单里"),
    "inject": ("DI 容器", "依赖注入，绑定关系在运行期确定",
               "确认注入目标是否受本次改动影响"),
    "Provide": ("DI 容器", "依赖注入标记，绑定关系在容器配置里",
                "确认容器配置是否在改造清单里"),
    "resolve": ("DI 容器", "从容器解析实例，实现类在运行期决定",
                "确认容器绑定表是否需要同步"),
    "add_provider": ("DI 容器", "注册提供者，绑定关系在运行期",
                     "确认绑定表是否需要同步"),
}


def py_blind(tree: ast.AST, text: str) -> list[dict]:
    spots: list[dict] = []

    def add(category: str, line: int, code: str, why: str, action: str) -> None:
        spots.append({"category": category, "line": line,
                      "code": code or _line_of(text, line), "why": why, "action": action})

    for node in ast.walk(tree):
        # --- 按名字反射
        if isinstance(node, ast.Call):
            fn = _dec_name(node.func)
            short = fn.rsplit(".", 1)[-1]
            info = PY_CATEGORY.get(short)
            # resolve / add_provider 太泛：Path.resolve()、list.add 都会命中，
            # 只有接收者像容器时才当 DI
            if info and short in ("resolve", "add_provider"):
                recv = fn.rsplit(".", 1)[0].lower() if "." in fn else ""
                if not any(k in recv for k in ("container", "injector", "provider",
                                               "bean", "di", "registry")):
                    info = None
            if info and short in ("getattr", "setattr"):
                # getattr(x, "literal") 仍算反射，但风险低一档，照样列出
                add(info[0], node.lineno, _line_of(text, node.lineno), info[1], info[2])
            elif info:
                add(info[0], node.lineno, _line_of(text, node.lineno), info[1], info[2])
            # **kwargs 转发：接收方决定真正调用了谁
            if any(kw.arg is None for kw in node.keywords):
                add("参数转发", node.lineno, _line_of(text, node.lineno),
                    "以 **kwargs 转发，被调函数与参数名在运行期决定",
                    "确认下游接收方是否受参数改名影响")

        # --- Provide["xxx"] 这种下标式注入标记
        if isinstance(node, ast.Subscript):
            name = _dec_name(node.value)
            if name.rsplit(".", 1)[-1] in ("Provide", "Dependency", "Inject"):
                add("DI 容器", node.lineno, _line_of(text, node.lineno),
                    "作为注入标记被下标引用，绑定关系在容器配置里",
                    "确认容器绑定表是否在改造清单里")

        # --- 装饰器形式的注入
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                short = _dec_name(dec).rsplit(".", 1)[-1]
                if short in ("inject", "autowired", "Autowired", "provider"):
                    add("DI 容器", dec.lineno, _line_of(text, dec.lineno),
                        f"@{short} 在运行期注入依赖，参数由容器提供",
                        "确认被注入的依赖是否在改造清单里")

        # --- 动态类创建
        if isinstance(node, ast.Call) and _dec_name(node.func) == "type" and len(node.args) >= 3:
            add("反射", node.lineno, _line_of(text, node.lineno),
                "运行期创建类，类名与属性由参数决定", "确认是否与本次改动的类相关")

        # --- 魔术方法：拦掉属性访问
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name in ("__getattr__", "__getattribute__", "__setattr__"):
            add("反射", node.lineno, _line_of(text, node.lineno),
                f"定义了 {node.name}，属性访问被接管，静态调用关系不可信",
                "人工确认该类的属性访问约定")

        # --- 字符串拼出来的路由 / 路径
        #
        # 只看字面量片段，不看源码原文：`Path / "x"` 的除号、日志里写的 "a/b"
        # 都不是路由，靠 `if "/" in line` 会把它们全误判。
        if isinstance(node, ast.JoinedStr):
            # 只看首个字面量：路由是 f"/api/{x}"，中间那些 " / " 只是分隔符
            head = node.values[0] if node.values else None
            parts = [head.value] if isinstance(head, ast.Constant) and \
                isinstance(head.value, str) else []
            hit = next((x for x in parts if _looks_like_path(x)), None)
            if hit is not None:
                add("动态路由", node.lineno, _line_of(text, node.lineno),
                    f"f-string 拼装路径（字面量 “{hit[:40]}”），静态无法确定最终字符串",
                    "人工确认拼装结果与接口契约文档是否一致")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left_const = _const_str(node.left)
            right_const = _const_str(node.right)
            hit = None
            for c in (left_const, right_const):
                if c and _looks_like_path(c):
                    hit = c
                    break
            if hit is not None and (left_const is None or right_const is None):
                add("动态路由", node.lineno, _line_of(text, node.lineno),
                    f"字符串拼接路径（字面量 “{hit[:40]}”），静态只能拿到一半",
                    "人工确认最终路径")

    # --- 装饰器声明的动态路由
    _, dyn_routes = py_routes(tree, text)
    for item in dyn_routes:
        add("动态路由", item["line"], item.get("expr", ""),
            f"@{item['decorator']} 的路径不是字面量，路由表在运行期确定",
            "人工核对最终 URL 并补进接口契约清单")

    # 同一个位置可能被多条规则命中（装饰器参数本身就是字符串拼接），按行+类别去重
    seen, unique = set(), []
    for spot in spots:
        key = (spot["category"], spot["line"], spot["code"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(spot)
    return sorted(unique, key=lambda s: s["line"])


def index_python(path: Path) -> dict:
    text = _read(path)
    if not text.strip():
        return {"engine": "ast", "defs": [], "imports": [], "routes": [], "blind": []}
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return {"engine": "ast-failed", "error": f"第 {exc.lineno} 行语法错误",
                "defs": [], "imports": [], "routes": [], "blind": []}

    routes, _ = py_routes(tree, text)
    return {
        "engine": "ast",
        "defs": py_defs(tree),
        "imports": py_imports(tree),
        "routes": routes,
        "blind": py_blind(tree, text),
    }


# --------------------------------------------------------------------------- 其他语言
#
# 这里只有一份符号正则，devloop 也用它（`symbol_names`），不再各写一套。
# 三种语言各管各的写法，避免"一套正则通吃"导致的互相误伤。
#
# Java / TS 不是拿正则逐行扫的：先用 `_segments` 把源码切成「声明段」，
# 再对段文本匹配。逐行有三个硬伤，切段后一并消失：
#
#   public R f() { return null; }       单行方法体，行尾不是 `{`
#   public R page(                      多行签名，名字在上一行、括号在下一行
#           @RequestParam String kw) {
#   @SuppressWarnings({"a", "b"})       注解数组里的花括号被当成分块
#
# 切段仍不是 AST（看不到继承、可见性、重载消歧），但已经不受换行和缩进影响。
# Go / 其余语言保持逐行：签名基本写在一行，动了收益小于风险。

CONTROL_WORDS = {
    "if", "for", "while", "switch", "catch", "return", "with", "do", "else", "try",
    "finally", "new", "await", "typeof", "delete", "throw", "case", "assert", "yield",
    "elseif", "unless", "until", "foreach", "using", "lock", "synchronized",
}

# 太通用的方法名：搜调用方时噪音远大于信息量，不进符号表
GENERIC_METHODS = {
    "tostring", "equals", "hashcode", "getclass", "clone", "finalize",
    "main", "run", "apply", "call", "builder", "build", "init", "setup",
    "get", "set", "add", "put", "next", "close", "open", "read", "write",
    "constructor", "destructor",
}

# 快进用：不含引号、反引号、注释符、花括号、圆括号、分号、方括号、换行的一段字符
_FAST_RUN = re.compile(r"[^\"'`/{}();\[\]\n]+")

# 段尾是这些字符说明话没说完，换行只是折行
_OPEN_TAIL = set("=+-*/%&|^<?:,.([")

# 换行后出现这些词，说明上一行还是同一个声明（throws 另起一行很常见）
_CONT_WORDS = {"throws", "implements", "extends", "instanceof", "catch", "finally"}


def _tail(buf: list[str], k: int = 1) -> str:
    """段尾最后 k 个非空白字符。"""
    out = ""
    for chunk in reversed(buf):
        out = chunk.rstrip() + out
        if len(out) >= k:
            break
    return out[-k:]


def _peek_word(text: str, i: int) -> tuple[str, str]:
    """下一处有效内容的第一个词和第一个字符（跳过空白与注释）。"""
    n = len(text)
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        break
    m = re.match(r"[A-Za-z_$][\w$]*", text[i:]) if i < n else None
    return (m.group() if m else ""), (text[i] if i < n else "")


def _segments(text: str) -> list[dict]:
    """把源码切成「声明段」——脱离逐行的那一步。

    一段从语句首个非空白字符起，到同层的 ``{``、``;``、断句的换行或文件尾为止：

    - ``kind="block"``：以 ``{`` 收尾，是方法 / 类型 / 匿名类的头
    - ``kind="stmt"``：以 ``;`` 或换行收尾，接口方法、抽象方法、箭头函数在这里

    字符串、字符、文本块、模板字符串、行注释、块注释整体跳过；圆括号、方括号内的
    ``{`` ``}`` ``;`` 不算边界，所以 ``@SuppressWarnings({"a"})`` 和
    ``for (int i = 0; i < n; i++)`` 都不会被切错。

    换行只在「上一段已经说完」时才断句（段尾不是运算符、下一行不是 ``throws``
    这类续写词），这是 JS/TS 省略分号后仍能一行一声明的关键；Java 的折行签名
    因为段尾是 ``(`` 或 ``,`` 不会误断。``line`` 是这段的首行。
    """
    segs: list[dict] = []
    buf: list[str] = []
    start = 0
    line = 1
    paren = brack = 0
    i, n = 0, len(text)

    def touch(tok: str) -> None:
        nonlocal start
        if not buf:
            start = line
        buf.append(tok)

    def push(kind: str) -> None:
        nonlocal buf
        body = " ".join("".join(buf).split())
        if body:
            segs.append({"kind": kind, "line": start, "text": body})
        buf = []

    while i < n:
        m = _FAST_RUN.match(text, i)
        if m:
            tok = m.group()
            if not buf:
                # 段首的空白不算正文，否则真正声明所在行会被前一行顶掉
                tok = tok.lstrip()
                if not tok:
                    i = m.end()
                    continue
            touch(tok)
            i = m.end()
            continue
        ch = text[i]
        if ch == "\n":
            line += 1
            if buf and paren == 0 and brack == 0:
                tail = _tail(buf, 2)
                word, nxt = _peek_word(text, i + 1)
                finished = tail[-1:] and tail[-1] not in _OPEN_TAIL \
                    and not tail.endswith("=>") \
                    and nxt not in ":.,)=" and word not in _CONT_WORDS
                if finished:
                    push("stmt")
                else:
                    buf.append(" ")
            elif buf:
                buf.append(" ")
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            end = n if j < 0 else j + 2
            line += text.count("\n", i, end)
            i = end
            continue
        if text.startswith('"""', i):          # 文本块：整体吞掉，行号照算
            j = text.find('"""', i + 3)
            end = n if j < 0 else j + 3
            touch('""')
            line += text.count("\n", i, end)
            i = end
            continue
        if ch in "\"'`":                        # 含模板字符串：${} 里的花括号不是边界
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == ch:
                    j += 1
                    break
                j += 1
            tok = text[i:j]
            touch(tok)
            line += tok.count("\n")
            i = j
            continue
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "[":
            brack += 1
        elif ch == "]":
            brack = max(0, brack - 1)
        elif paren == 0 and brack == 0:
            # 花括号和分号永远是边界：即使 buf 为空也要吃掉，
            # 否则 `}` 会被当成正文塞进下一段，把紧随其后的声明带歪
            if ch in "{;}":
                if buf:
                    push("block" if ch == "{" else "stmt")
                i += 1
                continue
        touch(ch)
        i += 1
    push("stmt")
    return segs


_ANNOT_RE = re.compile(r"@[\w.]+")


def _strip_annotations(s: str) -> str:
    """去掉声明前的注解：``@Override``、``@GetMapping("/x")``、``@SuppressWarnings({...})``。

    不清掉的话，注解里的括号和逗号会把方法正则搅乱——这也是原来只能靠
    「一行里同时有注解和方法」时漏识别的原因之一。
    """
    while True:
        m = _ANNOT_RE.match(s)
        if not m:
            return s.strip()
        i = m.end()
        if i < len(s) and s[i] == "(":
            depth = 0
            while i < len(s):
                if s[i] == "(":
                    depth += 1
                elif s[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
        s = s[i:].lstrip()


# 以这些词开头的段是语句，不是方法声明
STMT_NOISE = CONTROL_WORDS | {
    "return", "throw", "new", "package", "import", "assert", "break", "continue",
    "var", "goto", "delete", "await", "print", "println", "super", "this",
}


def _first_word(s: str) -> str:
    return s.split(" ", 1)[0] if s else ""


def _has_assign(s: str) -> bool:
    """段里有没有顶层赋值号——有就是字段初始化或赋值语句，不是方法声明。"""
    depth = 0
    for i, ch in enumerate(s):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0:
            prev = s[i - 1] if i else ""
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if nxt in "=>" or prev in "=!<>":
                continue
            return True
    return False


JVM_TYPE_RE = re.compile(
    r"^(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed|"
    r"strictfp)\s+)*(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")

JVM_METHOD_RE = re.compile(
    r"^(?:(?:public|private|protected|static|final|abstract|synchronized|native|"
    r"default|strictfp|transient|volatile)\s+)*"
    r"(?:<[^>]{1,120}>\s*)?"
    r"([A-Za-z_$][\w$.<>\[\],? ]{0,120}?)\s+([A-Za-z_$][\w$]*)\s*\(.*\)"
    r"\s*(?:throws[\w.,\s]+)?$")

JVM_CTOR_RE = re.compile(r"^(?:public|private|protected)\s+([A-Z][\w$]*)\s*\(.*\)$")

JVM_ROUTE_RE = re.compile(
    r"@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\(\s*"
    r"(?:(?:value|path)\s*=\s*)?[\"']([^\"']*)[\"']")

JS_TYPE_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(class|interface|enum|type)\s+([A-Za-z_$][\w$]*)")

JS_FUNC_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")

JS_ARROW_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*"
    r"(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*"
    r"(?::[^=]+)?=>")

JS_METHOD_RE = re.compile(
    r"^(?:(?:public|private|protected|static|async|readonly|abstract|override|"
    r"get|set)\s+)*([A-Za-z_$][\w$]*)\s*\(([^()]*)\)\s*(?::\s*[^{;]+)?$")

GO_TYPE_RE = re.compile(r"^type\s+([A-Za-z_][\w]*)\s+(?:struct|interface|func|map|chan|\[)")
GO_FUNC_RE = re.compile(r"^func\s+([A-Za-z_][\w]*)\s*(?:\[[^\]]*\])?\(")
GO_METHOD_RE = re.compile(
    r"^func\s*\(\s*[A-Za-z_][\w]*\s+\*?([A-Za-z_][\w]*)\s*\)\s*([A-Za-z_][\w]*)\s*\(")

# 兜底：C/Rust/PHP/Ruby 等，只认最常见的几种声明
OTHER_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:(?:public|private|protected|internal|static|final|"
    r"abstract|async|override|open|data|sealed|partial)\s+)*"
    r"(def|class|function|func|fn|interface|struct|enum|impl|trait|record|object|type)"
    r"\s+([A-Za-z_$][\w$]{2,})")


def _kind_of(keyword: str) -> str:
    return "class" if keyword in ("class", "interface", "struct", "enum", "trait",
                                  "record", "object", "impl", "type") else "function"


def _mk(name: str, short: str, kind: str, line: int) -> dict:
    return {"name": name, "short": short, "kind": kind, "line": line}


def defs_jvm(text: str) -> list[dict]:
    out = []
    for seg in _segments(text):
        head = _strip_annotations(seg["text"])
        if not head:
            continue
        m = JVM_TYPE_RE.match(head)
        if m:
            out.append(_mk(m.group(2), m.group(2), _kind_of(m.group(1)), seg["line"]))
            continue
        if seg["kind"] == "stmt":
            # 以 ; 收尾的段里，调用语句和字段初始化也长成声明样，先排掉
            if _first_word(head).lower() in STMT_NOISE or _has_assign(head):
                continue
        m = JVM_CTOR_RE.match(head)
        if m:
            out.append(_mk(m.group(1), m.group(1), "constructor", seg["line"]))
            continue
        m = JVM_METHOD_RE.match(head)
        if m and m.group(2).lower() not in CONTROL_WORDS:
            out.append(_mk(m.group(2), m.group(2), "method", seg["line"]))
    return out


def defs_js(text: str) -> list[dict]:
    out = []
    for seg in _segments(text):
        head = seg["text"]
        m = JS_TYPE_RE.match(head)
        if m:
            out.append(_mk(m.group(2), m.group(2), _kind_of(m.group(1)), seg["line"]))
            continue
        m = JS_FUNC_RE.match(head)
        if m:
            out.append(_mk(m.group(1), m.group(1), "function", seg["line"]))
            continue
        m = JS_ARROW_RE.match(head)
        if m:
            out.append(_mk(m.group(1), m.group(1), "function", seg["line"]))
            continue
        # 以 { 收尾的段是方法定义；以换行收尾的只认 TS 接口成员
        # （`detail(id: number): Promise<Dto>` 有类型标注，`foo(x)` 这种调用没有）
        if seg["kind"] == "stmt":
            if _first_word(head).lower() in STMT_NOISE or _has_assign(head) \
                    or ":" not in head:
                continue
        m = JS_METHOD_RE.match(head)
        if m and m.group(1).lower() not in CONTROL_WORDS:
            name = m.group(1)
            out.append(_mk(name, name, "method", seg["line"]))
    return out


def defs_go(text: str) -> list[dict]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        m = GO_METHOD_RE.match(line)
        if m:
            out.append(_mk(f"{m.group(1)}.{m.group(2)}", m.group(2), "method", i))
            continue
        m = GO_TYPE_RE.match(line)
        if m:
            out.append(_mk(m.group(1), m.group(1), "class", i))
            continue
        m = GO_FUNC_RE.match(line)
        if m:
            out.append(_mk(m.group(1), m.group(1), "function", i))
    return out


def defs_other(text: str) -> list[dict]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        m = OTHER_RE.match(line)
        if m:
            out.append(_mk(m.group(2), m.group(2), _kind_of(m.group(1)), i))
    return out


def defs_of_text(text: str, suffix: str) -> list[dict]:
    """按语言取符号。单一入口，devloop 的调用方分析也走这里。"""
    suffix = suffix.lower()
    if not text:
        return []
    if suffix in JVM_EXTS:
        return defs_jvm(text)
    if suffix in JS_EXTS:
        return defs_js(text)
    if suffix in GO_EXTS:
        return defs_go(text)
    return defs_other(text)


def symbol_names(text: str, suffix: str) -> list[str]:
    """给「找调用方」用的名字列表：通用方法名剔除，避免满屏噪音。"""
    names = []
    for d in defs_of_text(text, suffix):
        short = d["short"]
        if short.lower() in GENERIC_METHODS:
            continue
        names.append(short)
    return names


def _blind_patterns(suffix: str) -> list[tuple[str, str, str, str]]:
    common = [
        ("反射", r"\bClass\.forName\s*\(|\b\.invoke\s*\(|\bReflect\.",
         "按名字/反射调用，静态不可见", "人工确认目标方法与本次改动的关系"),
        ("动态导入", r"\bimport\s*\(\s*[^\"'`\s]|\brequire\s*\(\s*[`\"]?[^\"'`\s]*\+|"
                    r"\bimportlib|\b__import__\s*\(",
         "运行时按表达式导入模块", "grep 目标模块，确认是否指向本次改动"),
        # 注意：@Autowired 前面不能加 \b —— @ 是非单词字符，与前面的空格之间没有词边界
        ("DI 容器", r"@(?:Autowired|Resource|Inject|Value|Bean|Qualifier|ComponentScan)\b|"
                    r"\bgetBean\s*\(|\binject\s*\(|\bProvider<|\b@Injectable|"
                    r"providers\s*:|@RequiredArgsConstructor|@AllArgsConstructor",
         "依赖注入在运行期绑定实现", "确认容器绑定表是否在改造清单里"),
        ("动态路由", r"(?:route|get|post|put|delete|patch|use|HandleFunc|Handle)\s*\(\s*"
                     r"[^\"'`\s]|`[^`]*\$\{[^}]+\}[^`]*`",
         "路由路径不是字面量，运行期才确定", "人工核对最终 URL 并补进契约清单"),
        ("注册表", r"\bregister\w*\s*\(|\bREGISTRY\b|\bsubclasses\s*\(\s*\)|"
                   r"\badd_handler\s*\(",
         "注册表模式，名字与实现运行期绑定", "确认新增/改名的实现是否要在此登记"),
        ("参数转发", r"\bapply\s*\(|\bcall_user_func|\bforward\w*\s*\(",
         "参数整体转发，被调方与参数名运行期决定", "确认下游是否受参数改名影响"),
    ]
    if suffix in JS_EXTS:
        common.append(("反射", r"\bobj\[[^\]]+\]\s*\(|\b\[[A-Za-z_$][\w$]*\]\s*\(|\beval\s*\(",
                       "动态成员调用，属性名运行期确定", "人工确认调用了哪个方法"))
    return common


def index_generic(path: Path, suffix: str) -> dict:
    text = _read(path)
    if not text:
        return {"engine": "regex", "defs": [], "imports": [], "routes": [], "blind": []}

    blind = []
    for i, line in enumerate(text.splitlines(), 1):
        for cat, pat, why, action in _blind_patterns(suffix):
            if re.search(pat, line):
                blind.append({"category": cat, "line": i, "code": line.strip()[:160],
                              "why": why, "action": action})
                break

    routes = []
    if suffix in JVM_EXTS:
        for i, line in enumerate(text.splitlines(), 1):
            m = JVM_ROUTE_RE.search(line)
            if m:
                routes.append({"path": m.group(1), "line": i, "method": "ANY", "handler": ""})

    return {"engine": "regex", "defs": defs_of_text(text, suffix),
            "imports": [], "routes": routes, "blind": blind}


def index_file(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix in PY_EXTS:
        return index_python(path)
    if suffix in (JS_EXTS | JVM_EXTS | GO_EXTS | OTHER_EXTS):
        return index_generic(path, suffix)
    return {"engine": "none", "defs": [], "imports": [], "routes": [], "blind": []}


# --------------------------------------------------------------------------- 便捷入口

def py_defs_of(path: Path) -> list[str]:
    """给 devloop 的调用方分析用：只回名字列表，失败回空。"""
    info = index_file(path)
    return [d["name"] for d in info.get("defs", [])]


def blind_in_files(root: Path, files: list[str]) -> list[dict]:
    """汇总一批文件的盲区，按类别聚。"""
    out = []
    for f in files:
        info = index_file(root / f)
        for spot in info.get("blind", []):
            out.append({**spot, "file": f})
    return out


CATEGORY_ACTION_HINT = {
    "反射": "按字符串名访问，改名/移动不会报错",
    "动态导入": "模块名在运行期决定，grep 才找得到",
    "DI 容器": "实现由容器绑定，构造关系不在代码里",
    "动态路由": "URL 运行期拼装，接口清单可能对不上",
    "注册表": "注册关系是数据不是代码，加类不会自动生效",
    "参数转发": "参数整体透传，改名影响下游",
    "动态执行": "代码是字符串，静态完全看不见",
}
