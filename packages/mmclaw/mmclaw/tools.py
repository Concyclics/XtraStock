
from xtrastock.config import bootstrap_environment
bootstrap_environment()

import subprocess
import ast
import os
import locale
import json
import re
import sys
import textwrap
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

class ShellTool(object):
    TIMEOUT = 60

    @staticmethod
    def execute(command):
        """Executes a shell command and returns the output."""
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                timeout=ShellTool.TIMEOUT
            )
            output = result.stdout if result.returncode == 0 else result.stderr
            try:
                output = output.decode('utf-8')
            except UnicodeDecodeError:
                output = output.decode(locale.getpreferredencoding(False), errors='replace')
            return f"Return Code {result.returncode}:\n{output}"
        except Exception as e:
            return f"Error executing command: {str(e)}"

class AsyncShellTool(object):
    @staticmethod
    def execute(command):
        """Starts a long-running shell command in the background."""
        try:
            # Using Popen to start the process without waiting for it to finish.
            # Redirect stdout/stderr to DEVNULL to avoid cluttering.
            # No start_new_session so the process stays in the same process group.
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return f"Started background process (PID: {process.pid}) for command: {command}"
        except Exception as e:
            return f"Error starting background command: {str(e)}"

class FileTool(object):
    @staticmethod
    def read(path):
        """Reads a file and returns its content."""
        try:
            full_path = os.path.expanduser(path)
            with open(full_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f"Error reading file: {str(e)}"

    @staticmethod
    def write(path, content):
        """Writes content to a file."""
        try:
            full_path = os.path.expanduser(path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"Successfully wrote to {path}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class _SafeNumericEvaluator(ast.NodeVisitor):
    """Evaluate a deliberately small, Decimal-backed numeric expression language."""

    MAX_NODES = 120
    MAX_LIST_ITEMS = 200
    MAX_POWER = 100
    MAX_ADJUSTED_EXPONENT = 1000
    CONSTANTS = {
        "pi": Decimal("3.1415926535897932384626433832795028841971693993751"),
        "e": Decimal("2.7182818284590452353602874713526624977572470937000"),
    }

    def __init__(self, variables):
        self.variables = variables
        self.functions = {
            "abs": self._abs,
            "min": self._min,
            "max": self._max,
            "sum": self._sum,
            "round": self._round,
            "sqrt": self._sqrt,
            "ln": self._ln,
            "log": self._log,
            "log10": self._log10,
            "exp": self._exp,
            "mean": self._mean,
            "median": self._median,
            "stdev": self._stdev,
            "pstdev": self._pstdev,
            "pct_change": self._pct_change,
            "cagr": self._cagr,
            "weighted_average": self._weighted_average,
        }

    @classmethod
    def evaluate(cls, expression, variables):
        tree = ast.parse(expression, filename="<finance_calculator>", mode="eval")
        if sum(1 for _ in ast.walk(tree)) > cls.MAX_NODES:
            raise ValueError(f"expression exceeds {cls.MAX_NODES} AST nodes")
        return cls(variables).visit(tree)

    @classmethod
    def _checked(cls, value):
        if not isinstance(value, Decimal):
            raise ValueError("operation did not produce a number")
        if not value.is_finite():
            raise ValueError("result must be finite")
        if value and abs(value.adjusted()) > cls.MAX_ADJUSTED_EXPONENT:
            raise ValueError("numeric magnitude is too large")
        return value

    @staticmethod
    def _as_sequence(args):
        if len(args) == 1 and isinstance(args[0], list):
            return args[0]
        return list(args)

    @staticmethod
    def _require_decimal(value, label="argument"):
        if not isinstance(value, Decimal):
            raise ValueError(f"{label} must be numeric")
        return value

    @staticmethod
    def _require_vector(value, label="argument"):
        if not isinstance(value, list) or not value:
            raise ValueError(f"{label} must be a non-empty numeric list")
        if not all(isinstance(item, Decimal) for item in value):
            raise ValueError(f"{label} must contain only numbers")
        return value

    @classmethod
    def _power(cls, base, exponent):
        base = cls._require_decimal(base, "base")
        exponent = cls._require_decimal(exponent, "exponent")
        if abs(exponent) > cls.MAX_POWER:
            raise ValueError(f"absolute exponent exceeds {cls.MAX_POWER}")
        if exponent == exponent.to_integral_value():
            return cls._checked(base ** int(exponent))
        if base <= 0:
            raise ValueError("fractional powers require a positive base")
        return cls._checked((base.ln() * exponent).exp())

    def visit_Expression(self, node):
        return self.visit(node.body)

    def visit_Constant(self, node):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numeric literals are allowed")
        return self._checked(Decimal(str(node.value)))

    def visit_Name(self, node):
        if node.id in self.variables:
            return self.variables[node.id]
        if node.id in self.CONSTANTS:
            return self.CONSTANTS[node.id]
        raise ValueError(f"unknown variable: {node.id}")

    def visit_List(self, node):
        if len(node.elts) > self.MAX_LIST_ITEMS:
            raise ValueError(f"list exceeds {self.MAX_LIST_ITEMS} items")
        return [self.visit(item) for item in node.elts]

    def visit_Tuple(self, node):
        return self.visit_List(node)

    def visit_UnaryOp(self, node):
        value = self._require_decimal(self.visit(node.operand))
        if isinstance(node.op, ast.UAdd):
            return value
        if isinstance(node.op, ast.USub):
            return self._checked(-value)
        raise ValueError("unary operator is not allowed")

    def visit_BinOp(self, node):
        left = self._require_decimal(self.visit(node.left), "left operand")
        right = self._require_decimal(self.visit(node.right), "right operand")
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            result = left / right
        elif isinstance(node.op, ast.FloorDiv):
            result = left // right
        elif isinstance(node.op, ast.Mod):
            result = left % right
        elif isinstance(node.op, ast.Pow):
            return self._power(left, right)
        else:
            raise ValueError("binary operator is not allowed")
        return self._checked(result)

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name) or node.func.id not in self.functions:
            raise ValueError("function is not allowed")
        if node.keywords:
            raise ValueError("keyword arguments are not allowed")
        return self.functions[node.func.id](*[self.visit(arg) for arg in node.args])

    def generic_visit(self, node):
        raise ValueError(f"syntax is not allowed: {type(node).__name__}")

    def _abs(self, value):
        return self._checked(abs(self._require_decimal(value)))

    def _min(self, *args):
        values = self._as_sequence(args)
        if not values:
            raise ValueError("min requires at least one number")
        return min(self._require_decimal(value) for value in values)

    def _max(self, *args):
        values = self._as_sequence(args)
        if not values:
            raise ValueError("max requires at least one number")
        return max(self._require_decimal(value) for value in values)

    def _sum(self, *args):
        values = self._as_sequence(args)
        return self._checked(sum((self._require_decimal(value) for value in values), Decimal(0)))

    def _round(self, value, digits=Decimal(0)):
        value = self._require_decimal(value)
        digits = self._require_decimal(digits, "digits")
        if digits != digits.to_integral_value() or not (-12 <= digits <= 12):
            raise ValueError("round digits must be an integer from -12 to 12")
        quantum = Decimal(1).scaleb(-int(digits))
        return self._checked(value.quantize(quantum, rounding=ROUND_HALF_UP))

    def _sqrt(self, value):
        value = self._require_decimal(value)
        if value < 0:
            raise ValueError("sqrt requires a non-negative number")
        return self._checked(value.sqrt())

    def _ln(self, value):
        value = self._require_decimal(value)
        if value <= 0:
            raise ValueError("ln requires a positive number")
        return self._checked(value.ln())

    def _log(self, value, base=Decimal("2.7182818284590452353602874713527")):
        value = self._require_decimal(value)
        base = self._require_decimal(base, "base")
        if value <= 0 or base <= 0 or base == 1:
            raise ValueError("log requires value > 0 and base > 0, base != 1")
        return self._checked(value.ln() / base.ln())

    def _log10(self, value):
        return self._log(value, Decimal(10))

    def _exp(self, value):
        return self._checked(self._require_decimal(value).exp())

    def _mean(self, values):
        values = self._require_vector(values, "values")
        return self._checked(sum(values, Decimal(0)) / len(values))

    def _median(self, values):
        values = sorted(self._require_vector(values, "values"))
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return self._checked((values[middle - 1] + values[middle]) / 2)

    def _variance(self, values, sample):
        values = self._require_vector(values, "values")
        if sample and len(values) < 2:
            raise ValueError("stdev requires at least two values")
        mean = sum(values, Decimal(0)) / len(values)
        divisor = len(values) - 1 if sample else len(values)
        return sum(((value - mean) ** 2 for value in values), Decimal(0)) / divisor

    def _stdev(self, values):
        return self._checked(self._variance(values, True).sqrt())

    def _pstdev(self, values):
        return self._checked(self._variance(values, False).sqrt())

    def _pct_change(self, new_value, old_value):
        new_value = self._require_decimal(new_value, "new_value")
        old_value = self._require_decimal(old_value, "old_value")
        return self._checked((new_value - old_value) / old_value)

    def _cagr(self, begin_value, end_value, years):
        begin_value = self._require_decimal(begin_value, "begin_value")
        end_value = self._require_decimal(end_value, "end_value")
        years = self._require_decimal(years, "years")
        if begin_value <= 0 or end_value < 0 or years <= 0:
            raise ValueError("cagr requires begin > 0, end >= 0, and years > 0")
        if end_value == 0:
            return Decimal(-1)
        return self._checked(self._power(end_value / begin_value, Decimal(1) / years) - 1)

    def _weighted_average(self, values, weights):
        values = self._require_vector(values, "values")
        weights = self._require_vector(weights, "weights")
        if len(values) != len(weights):
            raise ValueError("values and weights must have the same length")
        total_weight = sum(weights, Decimal(0))
        if total_weight == 0:
            raise ValueError("weights must not sum to zero")
        return self._checked(sum((v * w for v, w in zip(values, weights)), Decimal(0)) / total_weight)


class SafeCalculatorTool(object):
    """Decimal-backed calculator that never executes arbitrary Python code."""

    MAX_EXPRESSION_CHARS = 800
    MAX_VARIABLES = 40
    VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

    @classmethod
    def _coerce_value(cls, value, label):
        if isinstance(value, bool):
            raise ValueError(f"{label} must be numeric")
        if isinstance(value, (int, float, str)):
            try:
                result = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError(f"{label} is not a valid number") from exc
            return _SafeNumericEvaluator._checked(result)
        if isinstance(value, (list, tuple)):
            if len(value) > _SafeNumericEvaluator.MAX_LIST_ITEMS:
                raise ValueError(f"{label} exceeds {_SafeNumericEvaluator.MAX_LIST_ITEMS} items")
            return [cls._coerce_value(item, f"{label}[{index}]") for index, item in enumerate(value)]
        raise ValueError(f"{label} must be a number or numeric list")

    @staticmethod
    def _format_decimal(value):
        if value == 0:
            return "0"
        return format(value.normalize(), "f")

    @classmethod
    def calculate(cls, expression, variables=None, precision=6, unit=None):
        expression = str(expression or "").strip()
        if not expression:
            return "Error: finance_calculator requires a non-empty expression."
        if len(expression) > cls.MAX_EXPRESSION_CHARS:
            return f"Error: finance_calculator expression exceeds {cls.MAX_EXPRESSION_CHARS} characters."
        try:
            if isinstance(precision, bool):
                raise ValueError("precision must be an integer from 0 to 12")
            precision = int(precision)
            if not 0 <= precision <= 12:
                raise ValueError("precision must be an integer from 0 to 12")
            raw_variables = {} if variables is None else variables
            if not isinstance(raw_variables, dict):
                raise ValueError("variables must be an object")
            if len(raw_variables) > cls.MAX_VARIABLES:
                raise ValueError(f"variables exceed {cls.MAX_VARIABLES} entries")
            parsed_variables = {}
            for name, value in raw_variables.items():
                if not cls.VARIABLE_RE.fullmatch(str(name)):
                    raise ValueError(f"invalid variable name: {name}")
                parsed_variables[str(name)] = cls._coerce_value(value, str(name))

            with localcontext() as context:
                context.prec = 50
                result = _SafeNumericEvaluator.evaluate(expression, parsed_variables)
                if not isinstance(result, Decimal):
                    raise ValueError("final result must be one number")
                result = _SafeNumericEvaluator._checked(result)
                quantum = Decimal(1).scaleb(-precision)
                rounded = result.quantize(quantum, rounding=ROUND_HALF_UP)

            payload = {
                "expression": expression,
                "variables": raw_variables,
                "result": cls._format_decimal(result),
                "rounded_result": format(rounded, f".{precision}f"),
                "precision": precision,
                "unit": str(unit)[:40] if unit else None,
                "numeric_engine": "decimal_50_digits",
                "allowed_functions": [
                    "abs", "min", "max", "sum", "round", "sqrt", "ln", "log", "log10", "exp",
                    "mean", "median", "stdev", "pstdev", "pct_change", "cagr", "weighted_average",
                ],
                "usage_note": "Show the formula and sourced inputs in the answer. Do not use this result to replace missing source data.",
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except (ArithmeticError, SyntaxError, TypeError, ValueError) as exc:
            return f"Error: finance_calculator rejected expression: {type(exc).__name__}: {exc}"


class FinanceDBTool(object):
    """Read-only access to the local finance corpus database."""

    ROOT = Path(os.environ.get("FINANCE_CORPUS_ROOT", Path(__file__).resolve().parents[2]))
    ENV_FILE = ROOT / ".env.finance-db"
    MAX_LIMIT = 300
    MAX_OUTPUT_CHARS = 24000
    # Include the on-demand embedding HTTP budget (3s connect + 90s read),
    # lexical/vector queries, and process startup. GPU idle shutdown is unchanged.
    DOCUMENT_RETRIEVE_TIMEOUT = 125
    RESEARCH_BUNDLE_TIMEOUT = 180
    ALLOWED_SCHEMAS = ("agent", "ai", "analytics", "company", "corpus", "kg", "macro", "market", "news", "ops", "research", "public")
    MUTATING_RE = re.compile(
        r"\b(insert|update|delete|alter|drop|truncate|create|grant|revoke|copy|vacuum|call|do|merge|refresh|reindex|cluster)\b",
        re.I,
    )
    DANGEROUS_SELECT_RE = re.compile(
        r"\b(pg_sleep|set_config|pg_terminate_backend|pg_cancel_backend|dblink|lo_import|lo_export|pg_read_file|pg_ls_dir|pg_stat_file)\b",
        re.I,
    )
    PLOT_DIR = Path(os.environ["XTRASTOCK_WORKSPACES_DIR"]) / "qq_agent/generated/plots"
    PLOT_TIMEOUT = 45

    @classmethod
    def _safe_filename(cls, filename, suffix=".png"):
        raw = str(filename or "").strip()
        if not raw:
            raw = f"chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        raw = re.sub(r"[^0-9A-Za-z._-]+", "_", raw).strip("._")
        if not raw:
            raw = f"chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if not raw.lower().endswith(suffix):
            raw += suffix
        return raw[:120]

    @classmethod
    def current_time(cls, timezone_name="Asia/Shanghai"):
        """Return current wall-clock time for resolving relative dates."""
        try:
            tz_name = timezone_name or "Asia/Shanghai"
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz_name = "Asia/Shanghai"
                tz = ZoneInfo(tz_name)
            now = datetime.now(tz)
            utc_now = datetime.now(timezone.utc)
            payload = {
                "timezone": tz_name,
                "iso": now.isoformat(timespec="seconds"),
                "date": now.date().isoformat(),
                "time": now.strftime("%H:%M:%S"),
                "weekday": now.strftime("%A"),
                "utc_iso": utc_now.isoformat(timespec="seconds"),
                "relative_date_guidance": {
                    "today": now.date().isoformat(),
                    "yesterday": (now.date() - timedelta(days=1)).isoformat(),
                },
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"Finance time error: {type(e).__name__}: {e}"

    @classmethod
    def plot_chart(cls, code, filename=None, width=10, height=6, dpi=160):
        """Run constrained seaborn/matplotlib plotting code and save a PNG."""
        if not str(code or "").strip():
            return "Finance plot error: empty plotting code"
        cls.PLOT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = cls.PLOT_DIR / cls._safe_filename(filename)
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        runner = textwrap.dedent(
            r"""
            import ast
            import json
            import sys
            from pathlib import Path

            ALLOWED_IMPORTS = {
                "json",
                "math",
                "datetime",
                "pandas",
                "numpy",
                "seaborn",
                "matplotlib",
                "matplotlib.pyplot",
            }
            BLOCKED_NAMES = {
                "open", "exec", "eval", "compile", "input", "__import__", "breakpoint",
                "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
            }
            BLOCKED_ATTRS = {
                "system", "popen", "spawn", "fork", "remove", "unlink", "rmdir", "rename",
                "replace", "chmod", "chown", "mkdir", "rmtree", "copy", "copyfile",
            }

            def validate(tree):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            root = alias.name.split(".", 1)[0]
                            if alias.name not in ALLOWED_IMPORTS and root not in ALLOWED_IMPORTS:
                                raise ValueError(f"import not allowed: {alias.name}")
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        root = module.split(".", 1)[0]
                        if module not in ALLOWED_IMPORTS and root not in ALLOWED_IMPORTS:
                            raise ValueError(f"import-from not allowed: {module}")
                    elif isinstance(node, ast.Call):
                        func = node.func
                        if isinstance(func, ast.Name) and func.id in BLOCKED_NAMES:
                            raise ValueError(f"blocked function: {func.id}")
                        if isinstance(func, ast.Attribute) and func.attr in BLOCKED_ATTRS:
                            raise ValueError(f"blocked method: {func.attr}")
                    elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                        raise ValueError("dunder attribute access is not allowed")

            spec = json.loads(sys.argv[1])
            code = spec["code"]
            output_path = Path(spec["output_path"])
            width = float(spec.get("width") or 10)
            height = float(spec.get("height") or 6)
            dpi = int(spec.get("dpi") or 160)
            font_paths = spec.get("font_paths") or []

            tree = ast.parse(code, filename="<finance_plot_chart>", mode="exec")
            validate(tree)

            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from matplotlib import font_manager
            import pandas as pd
            import numpy as np
            import seaborn as sns

            real_import = __import__

            def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
                if level != 0:
                    raise ImportError("relative imports are not allowed")
                root = str(name).split(".", 1)[0]
                if name not in ALLOWED_IMPORTS and root not in ALLOWED_IMPORTS:
                    raise ImportError(f"import not allowed: {name}")
                return real_import(name, globals, locals, fromlist, level)

            zh_font = "DejaVu Sans"
            for font_path in list(font_paths) + [
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            ]:
                path = Path(font_path)
                if path.exists():
                    font_manager.fontManager.addfont(str(path))
                    zh_font = font_manager.FontProperties(fname=str(path)).get_name()
                    break
            sns.set_theme(style="whitegrid", font=zh_font)
            plt.rcParams["font.sans-serif"] = [
                zh_font, "Noto Sans CJK SC", "DejaVu Sans", "SimHei", "Microsoft YaHei", "Arial Unicode MS"
            ]
            plt.rcParams["axes.unicode_minus"] = False
            plt.figure(figsize=(width, height))

            safe_builtins = {
                "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict, "enumerate": enumerate,
                "float": float, "int": int, "len": len, "list": list, "max": max, "min": min,
                "range": range, "round": round, "set": set, "sorted": sorted, "str": str, "sum": sum,
                "tuple": tuple, "zip": zip, "__import__": guarded_import,
            }
            namespace = {
                "__builtins__": safe_builtins,
                "pd": pd,
                "np": np,
                "sns": sns,
                "plt": plt,
                "output_path": str(output_path),
            }
            exec(compile(tree, "<finance_plot_chart>", "exec"), namespace, namespace)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            if not output_path.exists():
                if not plt.get_fignums():
                    raise RuntimeError("plot code created no matplotlib figure and did not save output_path")
                plt.tight_layout()
                plt.savefig(output_path, dpi=dpi, bbox_inches="tight")
            print(json.dumps({"path": str(output_path), "bytes": output_path.stat().st_size}, ensure_ascii=False))
            """
        )
        spec = {
            "code": str(code),
            "output_path": str(output_path),
            "width": width,
            "height": height,
            "dpi": dpi,
            "font_paths": [
                str(cls.ROOT / "MMClaw/workspaces/qq_agent/fonts/NotoSansCJKsc-Regular.otf"),
            ],
        }
        try:
            proc = subprocess.run(
                [cls._python_executable(), "-c", runner, json.dumps(spec, ensure_ascii=False)],
                cwd=str(cls.ROOT),
                capture_output=True,
                text=True,
                timeout=cls.PLOT_TIMEOUT,
            )
            if proc.returncode != 0:
                return (
                    f"Finance plot error: child process returned {proc.returncode}.\n"
                    f"{(proc.stderr or proc.stdout).strip()}"
                )
            payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
            return json.dumps(
                {
                    "path": payload["path"],
                    "bytes": payload["bytes"],
                    "next_step": "Call file_upload(path) to share this chart with the current chat or QQ group.",
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            return f"Finance plot error: {type(e).__name__}: {e}"

    @classmethod
    def _load_env(cls):
        from xtrastock.config import load_legacy_env
        return load_legacy_env()

    @classmethod
    def _validate_sql(cls, sql):
        cleaned = (sql or "").strip().rstrip(";").strip()
        if not cleaned:
            raise ValueError("empty SQL")
        if ";" in cleaned:
            raise ValueError("only one SQL statement is allowed")
        if cls.MUTATING_RE.search(cleaned):
            raise ValueError("mutating SQL is not allowed")
        if cls.DANGEROUS_SELECT_RE.search(cleaned):
            raise ValueError("unsafe SQL function is not allowed")
        if not re.match(r"^(select|with)\b", cleaned, re.I):
            raise ValueError("only SELECT/WITH queries are allowed")
        return cleaned

    @staticmethod
    def _json_default(value):
        return str(value)

    @classmethod
    def _format_rows(cls, rows, meta=None):
        payload = {"meta": meta or {}, "rows": rows}
        text = json.dumps(payload, ensure_ascii=False, default=cls._json_default, indent=2)
        if len(text) > cls.MAX_OUTPUT_CHARS:
            text = text[: cls.MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return text

    @classmethod
    def _python_executable(cls):
        project_python = cls.ROOT / sys.executable
        if project_python.exists():
            return str(project_python)
        return sys.executable

    @classmethod
    def _run_child(cls, args, timeout=30):
        helper = Path(__file__).resolve().with_name("finance_db_helper.py")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        try:
            proc = subprocess.run(
                [cls._python_executable(), str(helper), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cls.ROOT),
                env=env,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run kills and reaps its child on timeout. Do not let a
            # data-tool failure abort the whole turn as a model-provider error,
            # and never treat partial stdout or exception arguments as evidence.
            return (
                f"FinanceDB error: read-only data tool timed out after {timeout} seconds.\n"
                "IMPORTANT: this failed tool call is not evidence; do not infer or invent data from it.\n"
                "Use a narrower query or explain the data gap; this is not a model-service failure."
            )
        output = proc.stdout if proc.returncode == 0 else proc.stderr
        output = output.strip()
        if proc.returncode != 0:
            output = (
                f"FinanceDB error: child process returned {proc.returncode}.\n"
                "IMPORTANT: this failed tool call is not evidence; do not infer or invent data from it.\n"
                f"{output}"
            )
        elif any(marker in output for marker in ("Traceback (most recent call last)", "psycopg.errors.", "TimeoutExpired")):
            output = (
                "FinanceDB error: helper output contains an exception marker.\n"
                "IMPORTANT: this failed tool call is not evidence; retry with schema checks or state that data is unavailable.\n"
                f"{output}"
            )
        if len(output) > cls.MAX_OUTPUT_CHARS:
            output = cls._bound_child_output(output)
        return output or f"Return Code {proc.returncode}"

    @classmethod
    def _bound_child_output(cls, output):
        """Keep oversized V3 envelopes parseable and evidence entries intact."""
        try:
            payload = json.loads(output)
        except (ValueError, TypeError):
            return output[: cls.MAX_OUTPUT_CHARS] + "\n... [truncated]"

        def encode():
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        text = encode()
        if len(text) <= cls.MAX_OUTPUT_CHARS:
            return text
        if (isinstance(payload, dict) and isinstance(payload.get("meta"), dict)
                and isinstance(payload.get("rows"), list) and "evidence" not in payload):
            # Legacy screens are wrapped into a citation ledger after this
            # boundary. Retain complete ranked rows instead of treating a large
            # successful SELECT as an error (which disables the whole tool).
            meta, rows = payload["meta"], payload["rows"]
            available = len(rows)
            meta["output_compacted"] = True
            meta["rows_available"] = available
            meta.setdefault("warnings", []).append(
                "Only the leading ranked rows fit the response budget. "
                "Use a narrower scope to retrieve other candidates."
            )
            while rows:
                meta["row_count"] = len(rows)
                meta["rows_omitted_from_output"] = available - len(rows)
                text = encode()
                if len(text) <= cls.MAX_OUTPUT_CHARS:
                    return text
                rows.pop()
        if (isinstance(payload, dict) and isinstance(payload.get("meta"), dict)
                and isinstance(payload.get("evidence"), list)):
            meta = payload["meta"]
            evidence = payload["evidence"]
            available = len(evidence)
            # Detailed shared research already has citation-bound evidence. Do
            # not spend most of the budget on an uncitable duplicate in meta.
            shared=meta.get('shared_research')
            if isinstance(shared,dict):
                meta['shared_research']={k:v for k,v in shared.items() if k not in ('rows','watchlist')}
            meta["output_compacted"] = True
            meta["rows_omitted_from_output"] = len(payload.get("rows") or [])
            payload["rows"] = []  # Evidence retains the facts and citation IDs.
            meta["evidence_count_available"] = available
            meta.setdefault("warnings", []).append(
                "Output compacted to fit the tool budget. Use only the returned evidence; "
                "component counts describe the full query, not the returned evidence. "
                "Use a narrower query for omitted detail."
            )
            # Round-robin components before trimming. A large financial history
            # must not crowd all graph, factor and document evidence out of a
            # company bundle. Entries and their original citation IDs stay whole.
            groups = {}
            for entry in evidence:
                value = entry.get("value") or {}
                component = value.get("component") if isinstance(value, dict) else None
                key = component or entry.get("claim_type") or "other"
                groups.setdefault(key, []).append(entry)
            meta["evidence_components_available"] = {k: len(v) for k, v in groups.items()}
            evidence[:] = [entry for offset in range(max(map(len, groups.values()), default=0))
                           for group in groups.values() for entry in group[offset:offset + 1]]
            while True:
                meta["evidence_count_returned"] = len(evidence)
                meta["evidence_omitted_from_output"] = available - len(evidence)
                returned = {}
                for entry in evidence:
                    value = entry.get("value") or {}
                    component = value.get("component") if isinstance(value, dict) else None
                    key = component or entry.get("claim_type") or "other"
                    returned[key] = returned.get(key, 0) + 1
                meta["evidence_components_returned"] = returned
                text = encode()
                if len(text) <= cls.MAX_OUTPUT_CHARS:
                    return text
                if not evidence:
                    break
                evidence.pop()
        # Never pass a chopped JSON document to the evidence validator.
        return (
            "FinanceDB error: result exceeds the tool output budget.\n"
            "IMPORTANT: this failed tool call is not evidence; retry a narrower query with a lower limit."
        )

    @classmethod
    def _v3_child(cls, mode, spec, timeout=30):
        payload = dict(spec or {})
        payload.setdefault("request_id", str(uuid.uuid4()))
        return cls._run_child(
            [mode, str(cls.ROOT), json.dumps(payload, ensure_ascii=False)],
            timeout=timeout,
        )

    @classmethod
    def _v3_wrap(cls, raw, tool_name, claim_type, spec=None):
        """Convert a legacy parameterized result into the V3 evidence envelope."""
        try:
            payload = json.loads(raw)
        except Exception:
            return raw
        if "evidence" in payload and (payload.get("meta") or {}).get("tool_version"):
            return raw
        request_id = str((spec or {}).get("request_id") or uuid.uuid4())
        cutoff = str((spec or {}).get("decision_cutoff") or datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
        rows = payload.get("rows") or []
        legacy_meta = payload.get("meta") or {}
        evidence = []
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            evidence.append({
                "evidence_id": f"{request_id}:E{index:04d}",
                "claim_type": claim_type,
                "entity_id": row.get("asset_id") or row.get("entity_id") or row.get("stock_code"),
                "metric_id": row.get("metric_id") or row.get("metric_code") or row.get("factor_name") or tool_name,
                "value": row,
                "unit": row.get("unit"),
                "period_end": row.get("period_end") or row.get("report_date"),
                "as_of_date": row.get("as_of_date") or row.get("trade_date") or row.get("published_date"),
                "available_at": row.get("available_at") or row.get("published_at") or row.get("published_date"),
                "source_id": row.get("source_name") or row.get("source") or tool_name,
                "source_document_id": row.get("document_id") or row.get("source_document_id"),
                "quality_status": row.get("quality_status") or "unverified",
            })
        status = "ok" if rows else str(legacy_meta.get("status") or "unavailable")
        meta = {
            "request_id": request_id,
            "tool_version": "finance-agent:v3.0",
            "decision_cutoff": cutoff,
            "data_through": legacy_meta.get("data_through") or legacy_meta.get("latest_date")
                            or legacy_meta.get("latest_trade_date"),
            "status": status,
            "coverage_pct": legacy_meta.get("coverage_pct"),
            "quality_status": legacy_meta.get("quality_status") or ("unverified" if rows else "fail"),
            "warnings": legacy_meta.get("warnings") or [],
            "row_count": len(rows),
            "read_only": True,
            "legacy_meta": legacy_meta,
        }
        output = json.dumps({"meta": meta, "rows": rows, "evidence": evidence}, ensure_ascii=False, default=cls._json_default, indent=2)
        return cls._bound_child_output(output) if len(output) > cls.MAX_OUTPUT_CHARS else output

    @classmethod
    def query(cls, sql, limit=200):
        """Run one read-only SELECT/WITH SQL query against the local finance database."""
        try:
            cleaned = cls._validate_sql(sql)
            safe_limit = max(1, min(int(limit or 80), cls.MAX_LIMIT))
            return cls._run_child(["query", str(cls.ROOT), cleaned, str(safe_limit)])
        except Exception as e:
            return f"FinanceDB error: {type(e).__name__}: {e}"

    @classmethod
    def schema(cls, table=None, schema=None):
        """List finance database tables or columns for a specific table."""
        try:
            schema_filter = schema if schema in cls.ALLOWED_SCHEMAS else None
            if table:
                table_name = str(table).split(".")[-1]
                if "." in str(table):
                    left, right = str(table).split(".", 1)
                    if left in cls.ALLOWED_SCHEMAS:
                        table_name = right
                table_arg = f"{schema_filter}.{table_name}" if schema_filter else str(table)
            else:
                table_arg = ""
            return cls._run_child(["schema", str(cls.ROOT), schema_filter or "", table_arg])
        except Exception as e:
            return f"FinanceDB schema error: {type(e).__name__}: {e}"

    @classmethod
    def theme_map(cls, theme=None, limit=30):
        """Find AI hardware concepts/themes and related sample stocks."""
        spec = {"theme": theme or "", "limit": limit}
        return cls._run_child(["theme_map", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def price_movers(cls, days=5, threshold=0.2, direction="down", theme=None, limit=50):
        """Find stocks whose latest technical return moved beyond a threshold."""
        spec = {
            "days": days,
            "threshold": threshold,
            "direction": direction,
            "theme": theme or "",
            "limit": limit,
        }
        return cls._run_child(["price_movers", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def research_momentum(cls, days=30, theme=None, stock_code=None, limit=50):
        """Rank stocks by recent research coverage and positive/upgrade rating momentum."""
        spec = {
            "days": days,
            "theme": theme or "",
            "stock_code": stock_code or "",
            "limit": limit,
        }
        return cls._run_child(["research_momentum", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def screen_stocks(cls, **filters):
        """Screen AI-hardware-scored stocks by theme, valuation, research, and technical filters."""
        return cls._run_child(["screen_stocks", str(cls.ROOT), json.dumps(filters, ensure_ascii=False)])

    @classmethod
    def stock_profile(cls, stock_code=None, stock_name=None, days=30, limit=80):
        """Build a compact stock profile from theme, research, technical, and event data."""
        spec = {
            "stock_code": stock_code or "",
            "stock_name": stock_name or "",
            "days": days,
            "limit": limit,
        }
        return cls._run_child(["stock_profile", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def asset_snapshot(cls, as_of_date=None, stock_code=None, stock_name=None, limit=50):
        """Read the point-in-time cross-market stock analysis snapshot."""
        spec = {
            "as_of_date": as_of_date or "",
            "stock_code": stock_code or "",
            "stock_name": stock_name or "",
            "limit": limit,
        }
        return cls._run_child(["asset_snapshot", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def fundamental_analysis(cls, **spec):
        """Return source-reported financial statement trends for one company."""
        return cls._run_child(["fundamental_analysis", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def peer_compare(cls, **spec):
        """Compare a target stock with explicit or industry peers at one snapshot date."""
        return cls._run_child(["peer_compare", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def relative_valuation(cls, **spec):
        """Calculate point-in-time historical PE/PB valuation percentiles."""
        return cls._run_child(["relative_valuation", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def capital_flow_analysis(cls, **spec):
        """Return money-flow and structured market-event evidence for one stock."""
        return cls._run_child(["capital_flow_analysis", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def macro_context(cls, **spec):
        """Return bounded macroeconomic indicator history with source metadata."""
        return cls._run_child(["macro_context", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def factor_catalog(cls, **spec):
        """List factor definitions, lifecycle, score coverage, and latest validation."""
        return cls._run_child(["factor_catalog", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def factor_profile(cls, **spec):
        """Explain one stock's point-in-time factor percentiles and recent changes."""
        return cls._run_child(["factor_profile", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def factor_validation(cls, **spec):
        """Return bounded out-of-sample factor evaluation evidence."""
        return cls._run_child(["factor_validation", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def factor_readiness(cls, **spec):
        """Audit factor data health, OOS evidence, governance, and production blockers."""
        return cls._run_child(["factor_readiness", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def factor_attribution(cls, **spec):
        """Explain one stock's factor contributions and prediction eligibility."""
        return cls._run_child(["factor_attribution", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def knowledge_graph(cls, query=None, stock_code=None, relation_types=None, as_of_date=None,
                        start_date=None, end_date=None, known_at=None, knowledge_mode="public",
                        query_mode="snapshot", lookback_days=365, review_statuses=None,
                        directness=None, source_systems=None, require_evidence=False,
                        exclude_event_relations=False, limit=50):
        """Query bitemporal, evidence-backed full-market entity relationships."""
        spec = {
            "query": query or "", "stock_code": stock_code or "",
            "relation_types": relation_types or [], "as_of_date": as_of_date or "",
            "start_date": start_date or "", "end_date": end_date or "",
            "known_at": known_at or "", "knowledge_mode": knowledge_mode,
            "query_mode": query_mode,
            "lookback_days": lookback_days, "review_statuses": review_statuses or [],
            "directness": directness or [], "source_systems": source_systems or [],
            "require_evidence": require_evidence,
            "exclude_event_relations": exclude_event_relations, "limit": limit,
        }
        return cls._run_child(["knowledge_graph", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=60)

    @classmethod
    def graph_paths(cls, query=None, stock_code=None, target_query=None, relation_types=None,
                    as_of_date=None, known_at=None, knowledge_mode="public", direction="both",
                    max_depth=3, min_confidence=0.5, review_statuses=None,
                    directness=None, source_systems=None, require_evidence=False,
                    exclude_event_relations=False, limit=20):
        """Find bounded evidence-backed bitemporal paths of up to three edges."""
        spec = {
            "query": query or "", "stock_code": stock_code or "",
            "target_query": target_query or "", "relation_types": relation_types or [],
            "as_of_date": as_of_date or "", "known_at": known_at or "",
            "knowledge_mode": knowledge_mode, "direction": direction,
            "max_depth": max_depth, "min_confidence": min_confidence,
            "review_statuses": review_statuses or [], "directness": directness or [],
            "source_systems": source_systems or [], "require_evidence": require_evidence,
            "exclude_event_relations": exclude_event_relations, "limit": limit,
        }
        return cls._run_child(["graph_paths", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=60)

    @classmethod
    def event_calendar(cls, start_date=None, end_date=None, stock_code=None, event_types=None, include_reports=True, limit=100):
        """Return a unified dated calendar of reports, document events, and market events."""
        spec = {
            "start_date": start_date or "", "end_date": end_date or "",
            "stock_code": stock_code or "", "event_types": event_types or [],
            "include_reports": include_reports, "limit": limit,
        }
        return cls._run_child(["event_calendar", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=60)

    @classmethod
    def portfolio_risk(cls, holdings=None, portfolio_name=None, as_of_date=None, lookback_days=252):
        """Calculate deterministic historical portfolio risk from structured holdings."""
        spec = {
            "holdings": holdings or [], "portfolio_name": portfolio_name or "",
            "as_of_date": as_of_date or "", "lookback_days": lookback_days,
        }
        return cls._run_child(["portfolio_risk", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=90)

    @classmethod
    def watchlist_monitor(cls, stock_codes=None, watchlist_name=None, days=30, limit=100):
        """Return current technical, signal, research, and event state for a watchlist."""
        spec = {
            "stock_codes": stock_codes or [], "watchlist_name": watchlist_name or "",
            "days": days, "limit": limit,
        }
        return cls._run_child(["watchlist_monitor", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=60)

    @classmethod
    def strategy_backtest(cls, strategy="momentum_20d", start_date=None, end_date=None, stock_codes=None, top_n=20, rebalance_days=20, transaction_cost_bps=10):
        """Run a bounded point-in-time backtest for one whitelisted strategy."""
        spec = {
            "strategy": strategy, "start_date": start_date or "", "end_date": end_date or "",
            "stock_codes": stock_codes or [], "top_n": top_n,
            "rebalance_days": rebalance_days, "transaction_cost_bps": transaction_cost_bps,
        }
        return cls._run_child(["strategy_backtest", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=120)

    @classmethod
    def _workflow_component(cls, name, raw, max_rows=8):
        if raw.startswith(("FinanceDB error:", "Error:")):
            return {
                "tool": name, "status": "error", "meta": {}, "rows": [],
                "error": raw[:500],
            }
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {
                "tool": name, "status": "error", "meta": {}, "rows": [],
                "error": "tool output was not valid JSON",
            }
        rows = payload.get("rows") or []
        meta = payload.get("meta") or {}
        return {
            "tool": name,
            "status": "ok" if rows else "empty",
            "meta": meta,
            "rows": rows[:max_rows],
        }

    @staticmethod
    def _workflow_facts(row):
        priority = [
            "stock_code", "stock_name", "symbol", "name", "industry_name", "as_of_date", "dt",
            "published_date", "report_date", "observed_date", "report_period", "metric_name",
            "metric_value", "metric_yoy", "revenue", "net_profit_parent", "revenue_yoy",
            "net_profit_yoy", "eps", "roe", "pe_ttm", "pb", "forward_pe",
            "forecast_revenue_growth", "forecast_eps_growth", "return_1d", "return_5d",
            "return_20d", "net_inflow", "rating", "rating_change", "indicator_code", "value",
            "unit", "factor_name", "display_name", "family", "lifecycle_status", "score_date",
            "percentile", "zscore", "percentile_change_5obs", "test_start", "test_end",
            "observation_count", "rank_ic_mean", "q_value", "passed_gates",
            "prediction_eligible", "production_ready", "passing_oos_windows", "blockers",
            "depth", "path_confidence", "title", "report_title", "snippet", "source_name", "source_type", "url",
        ]
        facts = {}
        for key in priority:
            if key not in row or row[key] is None:
                continue
            value = row[key]
            if isinstance(value, str):
                value = value[:360]
            elif isinstance(value, list):
                value = value[:8]
            elif isinstance(value, dict):
                value = {str(k): v for k, v in list(value.items())[:10]}
            facts[key] = value
            if len(facts) >= 16:
                break
        if row.get("path_entities"):
            facts["path_entities"] = [
                {"name": item.get("name"), "entity_type": item.get("entity_type")}
                for item in row["path_entities"][:5]
            ]
        if row.get("path_edges"):
            facts["path_edges"] = [
                {
                    "relation_type": item.get("relation_type"),
                    "confidence": item.get("confidence"),
                    "source_system": item.get("source_system"),
                    "evidence_document_id": item.get("evidence_document_id"),
                    "evidence": str(item.get("evidence") or "")[:240],
                }
                for item in row["path_edges"][:4]
            ]
        return facts

    @classmethod
    def research_workflow(
        cls, workflow_type="company_due_diligence", stock_code=None, stock_name=None,
        theme=None, peer_stock_codes=None, as_of_date=None, days=180, limit=8,
    ):
        """Run a deterministic, evidence-ledger finance research workflow."""
        workflow = str(workflow_type or "company_due_diligence").strip().lower()
        if workflow not in {"company_due_diligence", "peer_comparison", "theme_research", "signal_explain"}:
            return "Error: unsupported workflow_type"
        safe_days = max(7, min(int(days or 180), 1095))
        safe_limit = max(3, min(int(limit or 8), 15))
        code = str(stock_code or "").strip()
        name = str(stock_name or "").strip()
        subject = str(theme or code or name).strip()
        if workflow == "theme_research" and not theme:
            return "Error: theme_research requires theme"
        if workflow != "theme_research" and not (code or name):
            return "Error: company workflows require stock_code or stock_name"

        calls = []
        if workflow == "company_due_diligence":
            calls = [
                ("finance_asset_snapshot", lambda: cls.asset_snapshot(as_of_date, code, name, 1), 2),
                ("finance_fundamental_analysis", lambda: cls.fundamental_analysis(stock_code=code, stock_name=name, periods=8), 8),
                ("finance_relative_valuation", lambda: cls.relative_valuation(stock_code=code, stock_name=name, as_of_date=as_of_date, lookback_days=min(safe_days * 3, 3650)), 4),
                ("finance_capital_flow_analysis", lambda: cls.capital_flow_analysis(stock_code=code, stock_name=name, days=min(safe_days, 365), limit=safe_limit), safe_limit),
                ("finance_forecast_trends", lambda: cls.forecast_trends(stock_code=code, stock_name=name, days=safe_days, limit=safe_limit), safe_limit),
                ("finance_factor_profile", lambda: cls.factor_profile(stock_code=code, stock_name=name, as_of_date=as_of_date, limit=safe_limit), safe_limit),
                ("finance_graph_paths", lambda: cls.graph_paths(stock_code=code, query=name, as_of_date=as_of_date, max_depth=2, limit=min(safe_limit, 8)), min(safe_limit, 8)),
                ("finance_document_retrieve", lambda: cls.document_retrieve(query=name or code, source_types=["news", "research_report"], days=safe_days, stock_code=code, intent="evidence", limit=safe_limit, backend="hybrid"), safe_limit),
                ("finance_macro_context", lambda: cls.macro_context(indicators=["GDP", "CPI", "PPI", "PMI", "M2", "LPR"], periods=3), 12),
            ]
        elif workflow == "peer_comparison":
            calls = [
                ("finance_peer_compare", lambda: cls.peer_compare(stock_code=code, stock_name=name, peer_stock_codes=peer_stock_codes or [], as_of_date=as_of_date, limit=safe_limit), safe_limit),
                ("finance_fundamental_analysis", lambda: cls.fundamental_analysis(stock_code=code, stock_name=name, periods=6), 6),
                ("finance_relative_valuation", lambda: cls.relative_valuation(stock_code=code, stock_name=name, as_of_date=as_of_date, lookback_days=min(safe_days * 3, 3650)), 4),
                ("finance_document_retrieve", lambda: cls.document_retrieve(query=name or code, source_types=["research_report"], days=safe_days, stock_code=code, intent="evidence", limit=safe_limit, backend="hybrid"), safe_limit),
            ]
        elif workflow == "theme_research":
            calls = [
                ("finance_theme_map", lambda: cls.theme_map(theme, safe_limit), safe_limit),
                ("finance_company_exposure", lambda: cls.company_exposure(query=theme, limit=safe_limit), safe_limit),
                ("finance_subindustry_trends", lambda: cls.subindustry_trends(subindustry=theme, days=min(safe_days, 365), limit=safe_limit), safe_limit),
                ("finance_graph_paths", lambda: cls.graph_paths(query=theme, as_of_date=as_of_date, max_depth=2, limit=min(safe_limit, 8)), min(safe_limit, 8)),
                ("finance_document_retrieve", lambda: cls.document_retrieve(query=theme, source_types=["news", "peoples_daily", "research_report"], days=safe_days, intent="evidence", limit=safe_limit, backend="hybrid"), safe_limit),
                ("finance_macro_context", lambda: cls.macro_context(indicators=["GDP", "CPI", "PPI", "PMI", "M2", "LPR"], periods=3), 12),
            ]
        else:
            calls = [
                ("finance_asset_snapshot", lambda: cls.asset_snapshot(as_of_date, code, name, 1), 2),
                ("finance_factor_attribution", lambda: cls.factor_attribution(stock_code=code, stock_name=name, as_of_date=as_of_date, limit=safe_limit), safe_limit),
                ("finance_factor_readiness", lambda: cls.factor_readiness(limit=20), 20),
                ("finance_factor_catalog", lambda: cls.factor_catalog(limit=20), 20),
                ("finance_graph_paths", lambda: cls.graph_paths(stock_code=code, query=name, as_of_date=as_of_date, max_depth=2, limit=min(safe_limit, 8)), min(safe_limit, 8)),
                ("finance_forecast_trends", lambda: cls.forecast_trends(stock_code=code, stock_name=name, days=safe_days, limit=safe_limit), safe_limit),
                ("finance_document_retrieve", lambda: cls.document_retrieve(query=name or code, source_types=["news", "research_report"], days=safe_days, stock_code=code, intent="evidence", limit=safe_limit, backend="hybrid"), safe_limit),
            ]

        components = []
        for tool_name, call, row_limit in calls:
            try:
                components.append(cls._workflow_component(tool_name, call(), row_limit))
            except Exception as exc:
                components.append({
                    "tool": tool_name, "status": "error", "meta": {}, "rows": [],
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })

        evidence_ledger = []
        for component_index, component in enumerate(components, start=1):
            for row_index, row in enumerate(component["rows"], start=1):
                evidence_ledger.append({
                    "evidence_id": f"E{component_index:02d}-{row_index:02d}",
                    "tool": component["tool"],
                    "source_type": row.get("source_type") or row.get("price_source") or row.get("data_source") or "database_fact",
                    "source_name": row.get("source_name") or row.get("institution"),
                    "date": row.get("published_date") or row.get("report_date") or row.get("as_of_date") or row.get("dt") or row.get("observed_date"),
                    "entity": row.get("stock_code") or row.get("symbol") or row.get("stock_name") or subject,
                    "title": row.get("title") or row.get("report_title") or row.get("metric_name") or row.get("indicator_code"),
                    "facts": cls._workflow_facts(row),
                })

        required = {
            "company_due_diligence": {"finance_asset_snapshot", "finance_fundamental_analysis", "finance_document_retrieve"},
            "peer_comparison": {"finance_peer_compare", "finance_document_retrieve"},
            "theme_research": {"finance_theme_map", "finance_company_exposure", "finance_document_retrieve"},
            "signal_explain": {"finance_asset_snapshot", "finance_factor_attribution", "finance_factor_readiness", "finance_graph_paths", "finance_document_retrieve"},
        }[workflow]
        available = {item["tool"] for item in components if item["status"] == "ok"}
        missing_required = sorted(required - available)
        errors = [item["tool"] for item in components if item["status"] == "error"]
        semantic_meta = next((item["meta"] for item in components if item["tool"] == "finance_document_retrieve"), {})
        verification = {
            "status": "complete" if not missing_required and not errors else ("partial" if evidence_ledger else "failed"),
            "required_components": sorted(required),
            "missing_required_components": missing_required,
            "error_components": errors,
            "evidence_count": len(evidence_ledger),
            "semantic_coverage_pct": semantic_meta.get("semantic_coverage_pct"),
            "warnings": [
                warning for warning in [
                    "Semantic history is still being backfilled; absence of semantic hits is not absence of evidence."
                    if semantic_meta.get("semantic_coverage_pct") is not None and float(semantic_meta.get("semantic_coverage_pct") or 0) < 95 else None,
                    "One or more required workflow components are missing; conclusions must be labeled partial."
                    if missing_required else None,
                    "One or more component tools failed; failed output is not evidence."
                    if errors else None,
                ] if warning
            ],
        }
        payload = {
            "meta": {
                "workflow_type": workflow, "subject": subject,
                "stock_code": code or None, "stock_name": name or None, "theme": theme or None,
                "as_of_date_requested": as_of_date or None, "lookback_days": safe_days,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True, "deterministic_workflow": True,
            },
            "verification": verification,
            "components": components,
            "evidence_ledger": evidence_ledger,
            "answer_contract": {
                "required_sections": ["data_freshness", "findings", "evidence", "risks_and_gaps"],
                "citation_key": "evidence_id",
                "prohibit": ["unsupported_numeric_claims", "guaranteed_returns", "failed_tool_output_as_evidence"],
            },
        }
        text = json.dumps(payload, ensure_ascii=False, default=cls._json_default, indent=2)
        if len(text) > cls.MAX_OUTPUT_CHARS:
            # Facts remain in the evidence ledger; remove duplicated component rows.
            for component in components:
                component["row_count_returned"] = len(component["rows"])
                component["rows"] = []
            payload["meta"]["output_compacted"] = True
            text = json.dumps(payload, ensure_ascii=False, default=cls._json_default, indent=2)
        if len(text) > cls.MAX_OUTPUT_CHARS:
            payload["evidence_ledger"] = evidence_ledger[:20]
            payload["meta"]["evidence_ledger_truncated"] = len(evidence_ledger) - 20
            payload["verification"]["warnings"].append(
                "The returned evidence ledger was capped at 20 rows; rerun a narrower workflow for more detail."
            )
            text = json.dumps(payload, ensure_ascii=False, default=cls._json_default, indent=2)
        return text

    @classmethod
    def entity_resolve(cls, query, asset_type=None, limit=10):
        """Resolve a stock, company, index, or concept to canonical local identifiers."""
        spec = {"query": query or "", "asset_type": asset_type or "", "limit": limit}
        return cls._run_child(["entity_resolve", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def time_context(
        cls,
        timezone_name="Asia/Shanghai",
        decision_cutoff=None,
        request_id=None,
        timezone=None,
    ):
        timezone_name = timezone or timezone_name
        return cls._v3_child("v3_time_context", {
            "timezone": timezone_name or "Asia/Shanghai",
            "decision_cutoff": decision_cutoff or "", "request_id": request_id or "",
        })

    @classmethod
    def resolve_entities(cls, query, asset_type=None, limit=10, decision_cutoff=None, request_id=None):
        spec = {
            "query": query or "", "asset_type": asset_type or "", "limit": limit,
            "decision_cutoff": decision_cutoff or "", "request_id": request_id or str(uuid.uuid4()),
        }
        return cls._v3_wrap(cls.entity_resolve(query, asset_type, limit), "finance_resolve_entities", "data_quality", spec)

    @classmethod
    def data_status(cls, dataset_keys=None, required_only=False, limit=50, decision_cutoff=None, request_id=None, scope="status", catalog_offset=0):
        return cls._v3_child("v3_data_status", {
            "dataset_keys": dataset_keys or [], "required_only": required_only, "limit": limit,
            "decision_cutoff": decision_cutoff or "", "request_id": request_id or "", "scope": scope, "catalog_offset": catalog_offset,
        }, timeout=5)

    @classmethod
    def market_screen_v3(cls, decision_cutoff=None, request_id=None, **filters):
        spec = dict(filters)
        spec.update({"decision_cutoff": decision_cutoff or "", "request_id": request_id or str(uuid.uuid4())})
        return cls._v3_wrap(cls.market_screen(**filters), "finance_market_screen", "calculation", spec)

    @classmethod
    def market_series_batch(cls, **spec):
        return cls._v3_child("v3_market_series_batch", spec, timeout=10)

    @classmethod
    def fundamentals_batch(cls, **spec):
        return cls._v3_child("v3_fundamentals_batch", spec, timeout=10)

    @classmethod
    def document_retrieve_v3(cls, decision_cutoff=None, request_id=None, **spec):
        request = dict(spec)
        request.update({"decision_cutoff": decision_cutoff or "", "request_id": request_id or str(uuid.uuid4())})
        return cls._v3_child("v3_document_retrieve", request, timeout=cls.DOCUMENT_RETRIEVE_TIMEOUT)

    @classmethod
    def factor_analysis(cls, **spec):
        return cls._v3_child("v3_factor_analysis", spec, timeout=10)

    @classmethod
    def graph_analysis(cls, **spec):
        return cls._v3_child("v3_graph_analysis", spec, timeout=10)

    @classmethod
    def research_bundle(cls, decision_cutoff=None, request_id=None, **spec):
        request = dict(spec)
        request.update({"decision_cutoff": decision_cutoff or "", "request_id": request_id or str(uuid.uuid4())})
        return cls._v3_child("v3_research_bundle", request, timeout=cls.RESEARCH_BUNDLE_TIMEOUT)

    @classmethod
    def research_intelligence(cls, **spec):
        return cls._v3_child("v3_research_intelligence", spec, timeout=10)

    @classmethod
    def claim_verify(cls, **spec):
        return cls._v3_child("v3_claim_verify", spec, timeout=5)

    @classmethod
    def data_context(cls, intent=None, metric_keys=None, dataset_keys=None, query_keys=None, include_health=True, limit=30):
        """Read metric definitions, query intents, data contracts, and fixed-table health."""
        spec = {
            "intent": intent or "",
            "metric_keys": metric_keys or [],
            "dataset_keys": dataset_keys or [],
            "query_keys": query_keys or [],
            "include_health": include_health,
            "limit": limit,
        }
        return cls._run_child(["data_context", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def market_screen(cls, **filters):
        """Screen the full point-in-time stock snapshot with bounded filters."""
        return cls._run_child(["market_screen", str(cls.ROOT), json.dumps(filters, ensure_ascii=False)])

    @classmethod
    def document_retrieve(cls, query=None, keywords=None, source_types=None, days=30, stock_code=None, intent="ranked", require_all_terms=False, limit=30, candidate_limit=500, snippet_chars=420, include_body=False, backend="auto"):
        """Use one intent-based entry point for normal document retrieval."""
        normalized_intent = str(intent or "ranked").strip().lower()
        if normalized_intent not in {"lookup", "ranked", "evidence"}:
            normalized_intent = "ranked"
        if normalized_intent == "lookup":
            return cls.document_search(
                query, keywords, source_types, days, stock_code, require_all_terms,
                limit, snippet_chars, include_body, backend,
            )
        if normalized_intent == "evidence":
            evidence_limit = min(int(limit or 10), 10)
            return cls.document_bm25_search(
                query, keywords, source_types, days, stock_code, evidence_limit,
                candidate_limit, include_body, snippet_chars, backend,
            )
        return cls.document_bm25_search(
            query, keywords, source_types, days, stock_code, limit, candidate_limit,
            include_body, snippet_chars, backend,
        )

    @classmethod
    def document_search(cls, query=None, keywords=None, source_types=None, days=30, stock_code=None, require_all_terms=False, limit=30, snippet_chars=420, include_body=False, backend="auto"):
        """Search news and research report documents by keyword/topic."""
        spec = {
            "query": query or "",
            "keywords": keywords or [],
            "source_types": source_types or [],
            "days": days,
            "stock_code": stock_code or "",
            "require_all_terms": require_all_terms,
            "limit": limit,
            "snippet_chars": snippet_chars,
            "include_body": include_body,
            "backend": backend or "auto",
        }
        return cls._run_child(["document_search", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=10)

    @classmethod
    def document_bm25_search(cls, query=None, keywords=None, source_types=None, days=30, stock_code=None, limit=30, candidate_limit=500, include_body_candidates=False, snippet_chars=420, backend="auto"):
        """Run relevance-ranked local news/research search; auto uses the bounded recent Meili cache, then FTS."""
        spec = {
            "query": query or "",
            "keywords": keywords or [],
            "source_types": source_types or [],
            "days": days,
            "stock_code": stock_code or "",
            "limit": limit,
            "candidate_limit": candidate_limit,
            "include_body_candidates": include_body_candidates,
            "snippet_chars": snippet_chars,
            "backend": backend or "auto",
        }
        return cls._run_child(["document_bm25_search", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=10)

    @classmethod
    def topic_clusters(cls, query=None, keywords=None, source_types=None, days=30, limit=30):
        """Cluster matched documents by co-occurring keywords and extracted event types."""
        spec = {
            "query": query or "",
            "keywords": keywords or [],
            "source_types": source_types or [],
            "days": days,
            "limit": limit,
        }
        return cls._run_child(["topic_clusters", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def document_evidence(cls, query=None, keywords=None, source_types=None, days=30, limit=30, snippet_chars=520, candidate_limit=500, include_body=False, backend="auto"):
        """Return evidence snippets from matched documents and extracted events."""
        spec = {
            "query": query or "",
            "keywords": keywords or [],
            "source_types": source_types or [],
            "days": days,
            "limit": limit,
            "snippet_chars": snippet_chars,
            "candidate_limit": candidate_limit,
            "include_body": include_body,
        }
        # The original evidence SQL can scan broad document bodies and has proven
        # fragile for wide policy-style queries. BM25 keeps a bounded candidate set.
        output = cls.document_bm25_search(
            query=query,
            keywords=keywords,
            source_types=source_types,
            days=days,
            limit=min(int(limit or 10), 10),
            candidate_limit=candidate_limit,
            include_body_candidates=include_body,
            snippet_chars=snippet_chars,
            backend=backend,
        )
        return "FinanceDB document_evidence: BM25-ranked evidence snippets.\n" + output

    @classmethod
    def subindustry_trends(cls, subindustry=None, days=30, limit=30):
        """Rank AI hardware subindustries by recent trend change."""
        spec = {
            "subindustry": subindustry or "",
            "days": days,
            "limit": limit,
        }
        return cls._run_child(["subindustry_trends", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def company_exposure(cls, query=None, term=None, subindustry=None, stock_code=None, stock_name=None, limit=50):
        """Find companies exposed to an AI hardware term or subindustry."""
        spec = {
            "query": query or "",
            "term": term or "",
            "subindustry": subindustry or "",
            "stock_code": stock_code or "",
            "stock_name": stock_name or "",
            "limit": limit,
        }
        return cls._run_child(["company_exposure", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def forecast_trends(cls, stock_code=None, stock_name=None, subindustry=None, days=180, limit=50):
        """Analyze forecast revisions from research report forecast rows."""
        spec = {
            "stock_code": stock_code or "",
            "stock_name": stock_name or "",
            "subindustry": subindustry or "",
            "days": days,
            "limit": limit,
        }
        return cls._run_child(["forecast_trends", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

    @classmethod
    def event_study(cls, query=None, keywords=None, event_type=None, stock_code=None, days=90, post_days=5, limit=80):
        """Measure post-event returns for extracted document events with linked stocks."""
        spec = {
            "query": query or "",
            "keywords": keywords or [],
            "event_type": event_type or "",
            "stock_code": stock_code or "",
            "days": days,
            "post_days": post_days,
            "limit": limit,
        }
        return cls._run_child(["event_study", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=90)

    @classmethod
    def financial_growth_screen(cls, report_period="2026Q1", metric="net_profit_parent_yoy", growth_gt=0.5, growth_lt=None, return_lte=None, return_gte=None, latest_date=None, price_adjustment="qfq", limit=50):
        """Screen actual extracted financial statement growth and exact materialized event returns."""
        spec = {
            "report_period": report_period or "2026Q1",
            "metric": metric or "net_profit_parent_yoy",
            "growth_gt": growth_gt,
            "growth_lt": growth_lt,
            "return_lte": return_lte,
            "return_gte": return_gte,
            "latest_date": latest_date or "",
            "price_adjustment": price_adjustment or "qfq",
            "limit": limit,
        }
        return cls._run_child(["financial_growth_screen", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=60)

    @classmethod
    def event_return(cls, stock_code=None, stock_name=None, event_type="financial_report", event_date=None, price_adjustment="qfq", limit=50):
        """Return materialized same-source price returns from an event date to latest trade date."""
        spec = {
            "stock_code": stock_code or "",
            "stock_name": stock_name or "",
            "event_type": event_type or "financial_report",
            "event_date": event_date or "",
            "price_adjustment": price_adjustment or "qfq",
            "limit": limit,
        }
        return cls._run_child(["event_return", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=60)

    @classmethod
    def fact_check_stock(cls, stock_code=None, stock_name=None, report_period="2026Q1", price_adjustment="qfq", days=180, limit=80):
        """Return actual metrics, event returns, and recent report rows for one stock."""
        spec = {
            "stock_code": stock_code or "",
            "stock_name": stock_name or "",
            "report_period": report_period or "2026Q1",
            "price_adjustment": price_adjustment or "qfq",
            "days": days,
            "limit": limit,
        }
        return cls._run_child(["fact_check_stock", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=60)

    @classmethod
    def target_price_history(cls, stock_codes=None, stock_code=None, stock_name=None, days=365, as_of_date=None, limit=50):
        """Return only explicit broker target prices parsed from research reports."""
        spec = {
            "stock_codes": stock_codes or [], "stock_code": stock_code or "",
            "stock_name": stock_name or "", "days": days,
            "as_of_date": as_of_date or "", "limit": limit,
        }
        return cls._run_child(["target_price_history", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=60)

    @classmethod
    def price_series(cls, stock_codes=None, stock_code=None, stock_name=None, start_date=None, end_date=None, adjustment="qfq", frequency="daily", limit=240):
        """Return a canonical local-trade-date OHLCV series with source deduplication."""
        spec = {
            "stock_codes": stock_codes or [], "stock_code": stock_code or "",
            "stock_name": stock_name or "", "start_date": start_date or "",
            "end_date": end_date or "", "adjustment": adjustment or "qfq",
            "frequency": frequency or "daily", "limit": limit,
        }
        return cls._run_child(["price_series", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=90)

    @classmethod
    def price_chart(cls, stock_codes=None, stock_code=None, stock_name=None, start_date=None, end_date=None, adjustment="qfq", frequency="weekly", normalize=False, filename=None):
        """Render a safe chart from the parameterized canonical price series."""
        raw = cls.price_series(
            stock_codes=stock_codes, stock_code=stock_code, stock_name=stock_name,
            start_date=start_date, end_date=end_date, adjustment=adjustment,
            frequency=frequency, limit=300,
        )
        try:
            payload = json.loads(raw)
        except Exception:
            return raw if raw.startswith("FinanceDB error:") else "Finance plot error: price series output was not valid JSON"
        rows = payload.get("rows") or []
        if not rows:
            return json.dumps({"meta": payload.get("meta") or {}, "path": None, "error": "no price rows"}, ensure_ascii=False)
        rows_json = json.dumps(rows, ensure_ascii=False, default=cls._json_default)
        title = "复权收盘价走势" if not normalize else "归一化价格走势（首期=100）"
        value_expr = (
            "df['plot_value'] = df.groupby('stock_code')['close'].transform(lambda s: s / s.iloc[0] * 100)"
            if normalize else "df['plot_value'] = df['close']"
        )
        code = "\n".join([
            "import json",
            f"rows = json.loads({json.dumps(rows_json, ensure_ascii=False)})",
            "df = pd.DataFrame(rows)",
            "df['trade_date'] = pd.to_datetime(df['trade_date'])",
            "df['close'] = pd.to_numeric(df['close'])",
            value_expr,
            "df['series'] = df['stock_name'].fillna(df['stock_code']) + ' (' + df['stock_code'] + ')'",
            "sns.lineplot(data=df, x='trade_date', y='plot_value', hue='series', linewidth=1.8)",
            f"plt.title({json.dumps(title, ensure_ascii=False)})",
            f"plt.ylabel({json.dumps('指数' if normalize else '价格（元）', ensure_ascii=False)})",
            "plt.xlabel('')",
            "plt.legend(title='')",
            "plt.tight_layout()",
        ])
        result = cls.plot_chart(code, filename=filename or "finance_price_series.png", width=11, height=6, dpi=160)
        try:
            rendered = json.loads(result)
            rendered["series_meta"] = payload.get("meta") or {}
            rendered["next_step"] = "Chart was rendered from finance_price_series; the kernel sends the file automatically."
            return json.dumps(rendered, ensure_ascii=False, indent=2)
        except Exception:
            return result

    @classmethod
    def limit_up_list(cls, as_of_date=None, exclude_ipo_window=True, limit=200):
        """Return a rule-based A-share limit-up list for one canonical trade date."""
        spec = {"as_of_date": as_of_date or "", "exclude_ipo_window": exclude_ipo_window, "limit": limit}
        return cls._run_child(["limit_up_list", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)], timeout=120)

    @classmethod
    def data_health(cls, limit=50):
        """Check freshness and operational health for finance corpus pipelines."""
        spec = {"limit": limit}
        return cls._run_child(["data_health", str(cls.ROOT), json.dumps(spec, ensure_ascii=False)])

class TimerTool(object):
    @staticmethod
    def wait(seconds):
        """Pauses execution for a specified number of seconds (cross-platform)."""
        import time
        try:
            secs = float(seconds)
            time.sleep(secs)
            return f"Waited for {secs} seconds."
        except Exception as e:
            return f"Timer error: {str(e)}"

class SessionTool(object):
    @staticmethod
    def reset():
        """Returns a signal string that the kernel will use to reset the session."""
        return "SESSION_RESET_SIGNAL"

class BrowserTool(object):
    import tempfile as _tempfile
    _tmp = _tempfile.gettempdir()
    CDP_URL = "http://localhost:9222"
    WS_FILE = f"{_tmp}/mmclaw_browser_ws.txt"
    DAEMON_SCRIPT = f"{_tmp}/mmclaw_browser_daemon.py"
    DAEMON_LOG = f"{_tmp}/mmclaw_browser_daemon.log"
    DEFAULT_SCREENSHOT = f"{_tmp}/mmclaw_browser_screenshot.png"
    DEFAULT_DATA_DIR = "~/.mmclaw/browser_data"
    _daemon_process = None

    @classmethod
    def _is_running(cls):
        import urllib.request
        try:
            urllib.request.urlopen(f"{cls.CDP_URL}/json/version", timeout=2)
            return True
        except Exception:
            return False

    @classmethod
    def _get_page(cls, pw):
        browser = pw.chromium.connect_over_cdp(cls.CDP_URL)
        if not browser.contexts:
            raise RuntimeError("No browser context found. Call browser_start first.")
        ctx = browser.contexts[0]  # always the persistent context from launch_persistent_context
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return browser, page

    @classmethod
    def start(cls, user_data_dir=None):
        if cls._is_running():
            return "Browser already running."
        import subprocess, sys, time, os, textwrap
        data_dir = os.path.join(os.path.expanduser(user_data_dir or cls.DEFAULT_DATA_DIR), "chromium")
        os.makedirs(data_dir, exist_ok=True)
        daemon_code = textwrap.dedent(f"""\
            from playwright.sync_api import sync_playwright
            import os, time, json
            WS_FILE = {repr(cls.WS_FILE)}
            DATA_DIR = {repr(data_dir)}

            # Patch Chrome Preferences to suppress "Restore pages?" dialog.
            # Chrome shows it when exit_type != "Normal" (i.e. it thinks it crashed).
            prefs_path = os.path.join(DATA_DIR, "Default", "Preferences")
            if os.path.exists(prefs_path):
                try:
                    prefs = json.loads(open(prefs_path).read())
                    prefs.setdefault("profile", {{}})["exit_type"] = "Normal"
                    prefs.setdefault("profile", {{}})["crashed"] = False
                    open(prefs_path, "w").write(json.dumps(prefs))
                except Exception:
                    pass

            try:
                with sync_playwright() as pw:
                    context = pw.chromium.launch_persistent_context(
                        DATA_DIR,
                        headless=False,
                        args=["--remote-debugging-port=9222", "--no-first-run", "--no-default-browser-check"],
                    )
                    open(WS_FILE, "w").write("http://localhost:9222")
                    print("Browser ready.", flush=True)
                    while os.path.exists(WS_FILE):
                        time.sleep(1)
                    context.close()
                print("Browser stopped.", flush=True)
            except Exception as e:
                print(f"ERROR: {{e}}", flush=True)
                raise
        """)
        with open(cls.DAEMON_SCRIPT, "w") as f:
            f.write(daemon_code)
        cls._daemon_process = subprocess.Popen(
            [sys.executable, cls.DAEMON_SCRIPT],
            stdout=open(cls.DAEMON_LOG, "w"), stderr=subprocess.STDOUT
        )
        log_pos = 0
        for _ in range(60):  # up to 30s — first launch after fresh install can be slow
            time.sleep(0.5)
            if os.path.exists(cls.DAEMON_LOG):
                with open(cls.DAEMON_LOG) as f:
                    f.seek(log_pos)
                    chunk = f.read()
                    if chunk:
                        print(chunk, end='', flush=True)
                        log_pos = f.tell()
            if cls._is_running():
                return "OK: Browser started."
        log = open(cls.DAEMON_LOG).read() if os.path.exists(cls.DAEMON_LOG) else "(no log)"
        return f"ERROR: Browser did not start within 30s.\n{log}"

    @classmethod
    def stop(cls):
        import os, time
        if not cls._is_running():
            if os.path.exists(cls.WS_FILE):
                os.remove(cls.WS_FILE)
            return "Browser is not running."
        if os.path.exists(cls.WS_FILE):
            os.remove(cls.WS_FILE)
        time.sleep(2)
        return "OK: Browser stopped."

    @classmethod
    def navigate(cls, url):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser, page = cls._get_page(pw)
                page.goto(url, wait_until="domcontentloaded")
                result = f"Title: {page.title()}\nURL: {page.url}"

            return result
        except Exception as e:
            return f"ERROR: {e}"

    @classmethod
    def click(cls, selector):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser, page = cls._get_page(pw)
                page.locator(selector).first.click()
                page.wait_for_load_state("domcontentloaded")
                result = f"Clicked. Title: {page.title()}\nURL: {page.url}"

            return result
        except Exception as e:
            return f"ERROR: {e}"

    @classmethod
    def fill(cls, selector, text):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser, page = cls._get_page(pw)
                page.locator(selector).fill(text)

            return "OK: filled."
        except Exception as e:
            return f"ERROR: {e}"

    @classmethod
    def get_text(cls, selector=None):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser, page = cls._get_page(pw)
                text = page.locator(selector).first.inner_text() if selector else page.inner_text("body")

            return text
        except Exception as e:
            return f"ERROR: {e}"

    @classmethod
    def screenshot(cls, path=None):
        try:
            from playwright.sync_api import sync_playwright
            save_path = path or cls.DEFAULT_SCREENSHOT
            with sync_playwright() as pw:
                browser, page = cls._get_page(pw)
                page.screenshot(path=save_path, full_page=True)

            return f"OK: {save_path}"
        except Exception as e:
            return f"ERROR: {e}"


class UpgradeTool(object):
    @staticmethod
    def upgrade():
        """Upgrades mmclaw via pip, then restarts the current process in-place."""
        import sys
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", "mmclaw"],
                capture_output=True,
                timeout=120
            )
            output = result.stdout + result.stderr
            try:
                output = output.decode('utf-8')
            except UnicodeDecodeError:
                output = output.decode(locale.getpreferredencoding(False), errors='replace')
            if result.returncode != 0:
                return f"Upgrade failed:\n{output}"
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            return f"Upgrade error: {str(e)}"
