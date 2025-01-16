import dataclasses
import os
import shlex
import subprocess
import time
from pathlib import Path

from consts import CUDA_FLAGS, ExitCode


@dataclasses.dataclass
class CompileResult:
    # fmt: off
    nvcc_found: bool    # did we find nvcc?
    nvcc_version: str   # the result of nvcc --version
    success: bool       # did it compile successfully
    command: str        # the command that was run to compile the code
    stdout: str         # standard output produced by the compiler
    stderr: str         # standard error produced by the compiler
    exit_code: int      # exit code produced by the compiler
    # fmt: on


@dataclasses.dataclass
class RunResult:
    # fmt: off
    success: bool       # did the compiled executable run successfully
    passed: bool        # did it pass all tests
    command: str        # the command that was run to compile the code
    stdout: str         # standard output produced by the compiler
    stderr: str         # standard error produced by the compiler
    exit_code: int      # exit code produced by the compiler
    duration: float     # execution time (NOT kernel duration)
    result: dict        # dictionary with the results generated by the tester
    # fmt: on


@dataclasses.dataclass
class FullResult:
    # fmt: off
    success: bool                  # did the runner (github/modal) execute successfully
    error: str                     # if not success, an error message
    compile: CompileResult | None  # results of compilation
    run: RunResult | None          # results of running
    # fmt: on


def _make_cmd(args: list[str]):
    return " ".join(map(shlex.quote, args))


def compile_cuda_script(  # # noqa: C901
    files: list[str],
    arch: int = None,
    include_dirs: list[str] = None,
    verbose: bool = False,
) -> CompileResult:
    """
    Compiles a set of cuda files with nvcc.

    Args:
        files: List of files to compile.
        arch: Architecture to compile for. If None, uses `native`
        include_dirs: additional include directories to supply to nvcc
        verbose: whether to print progress or be silent
        seed: Seed value to use for generating test cases
    Returns:
        A `CompileResult` that summarizes the compilation process.

    """
    if include_dirs is None:
        include_dirs = []

    if verbose:
        print_ = print
    else:
        print_ = lambda *args, **kwargs: None  # noqa

    # Check CUDA is available and installed correctly
    print_("[CUDA Env Check]")
    try:
        # these check cuda compiler is also available
        nvcc = subprocess.check_output(["which", "nvcc"], encoding="utf-8").strip()
        nvcc_version = subprocess.check_output(["nvcc", "--version"], encoding="utf-8")
    except subprocess.CalledProcessError as e:
        return CompileResult(
            nvcc_found=False,
            success=False,
            nvcc_version="",
            command=_make_cmd(e.cmd),
            stdout=e.stdout,
            stderr=e.stderr,
            exit_code=e.returncode,
        )

    if arch is None:
        ARCH = "-arch=native"
    else:
        ARCH = f"-gencode=arch=compute_{arch},code=sm_{arch}"

    command = [nvcc] + CUDA_FLAGS + include_dirs + files + [ARCH, "-o", "eval.out"]

    print_("[Compiling]")
    try:
        compile_process = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        return CompileResult(
            nvcc_found=True,
            success=False,
            nvcc_version=nvcc_version,
            command=_make_cmd(e.cmd),
            stdout=e.stdout,
            stderr=e.stderr,
            exit_code=e.returncode,
        )

    return CompileResult(
        nvcc_found=True,
        success=True,
        nvcc_version=nvcc_version,
        command=_make_cmd(compile_process.args),
        stdout=compile_process.stdout,
        stderr=compile_process.stderr,
        exit_code=compile_process.returncode,
    )


def run_program(args: list[str], seed: int) -> RunResult:
    # set up a pipe so the tester can communicate its verdict with us
    env = os.environ.copy()
    pipe_read, pipe_write = os.pipe()
    env["POPCORN_FD"] = str(pipe_write)
    env["POPCORN_SEED"] = str(seed)

    execution_start_time = time.perf_counter()
    run_process = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        pass_fds=[pipe_write],
    )
    execution_end_time = time.perf_counter()

    # terminate output writing
    os.close(pipe_write)
    # and fetch pipe's content
    result = os.fdopen(pipe_read, "r").read()

    result_dict = {}
    for line in result.splitlines():
        key, _, value = line.partition(":")
        if key != "" or value != "":
            result_dict[key.strip()] = value.strip()

    return RunResult(
        success=(
            run_process.returncode == ExitCode.SUCCESS
            or run_process.returncode == ExitCode.VALIDATE_FAIL
        ),
        passed=result_dict.get("check", None) == "pass",
        command=_make_cmd(run_process.args),
        stdout=run_process.stdout,
        stderr=run_process.stderr,
        exit_code=run_process.returncode,
        duration=execution_end_time - execution_start_time,
        result=result_dict,
    )


def run_cuda_script(  # # noqa: C901
    sources: dict[str, str],
    headers: dict[str, str] = None,
    arch: int = None,
    include_dirs: list[str] = None,
    seed: int = 42,
) -> tuple[CompileResult, RunResult]:
    """
    Executes the provided CUDA kernel in an isolated environment

    Args:
        sources: The source files to compile. Mapping file name to content.
        headers: Additional header files to create for the compile run.
            Mapping of file name to file contents. These files will _not_ be added to the
            compile command.
        arch: The arch code for the compute/sm versions. If None, native arch is used.
        include_dirs: Additional include directories, e.g., for thunderkittens/cutlass etc
        seed: Random seed to initialize the RNG for testing

    Returns:
        tuple[CompileResult, RunResult]: CUDA compile/eval result information
    """
    if include_dirs is None:
        include_dirs = []

    try:
        # Write submission files to directory
        for source, content in sources.items():
            Path(source).write_text(content)

        for header, content in headers.items():
            Path(header).write_text(content)

        compile_result = compile_cuda_script(
            files=list(sources.keys()),
            arch=arch,
            include_dirs=include_dirs,
            verbose=True,
        )

        if not compile_result.success:
            return compile_result, RunResult(
                success=False,
                passed=False,
                command="",
                stdout="",
                stderr="",
                exit_code=-1,
                duration=0.0,
                result={},
            )

    # cleaning up all source files _before_ we let the user code run, just in
    # case there's something in there that the user isn't supposed to snoop
    finally:
        tmp_files = list(sources.keys()) + list(headers.keys())
        for f in tmp_files:
            if os.path.exists(f):
                os.remove(f)

    run_result = run_program(["./eval.out"], seed=seed)
    return compile_result, run_result


def run_pytorch_script(  # noqa: C901
    sources: dict[str, str],
    main: str,
    arch: int = None,
    seed: int = 42,
) -> RunResult:
    """
    Executes the provided PyTorch GPU kernel in an isolated environment

    Args:
        sources: Files to generate
        main: Which file to run. Must be one of the keys in sources.
        arch: The arch code for the compute/sm versions.
        seed: Random seed to initialize the RNG for testing

    Returns:
        RunResult
    """
    try:
        assert main in sources.keys()

        # Write submission files to directory
        for source, content in sources.items():
            Path(source).write_text(content)
        return run_program(["python", main], seed=seed)

    finally:
        for f in sources.keys():
            if os.path.exists(f):
                os.remove(f)


def run_config(config: dict):
    if config["lang"] == "py":
        run_result = run_pytorch_script(
            sources=config["sources"], main=config["main"], arch=config.get("arch", None)
        )
        return FullResult(success=True, error="", compile=None, run=run_result)
    elif config["lang"] == "cu":
        comp, run = run_cuda_script(
            sources=config["sources"],
            headers=config.get("headers", {}),
            arch=config.get("arch", None),
            include_dirs=config.get("include_dirs", []),
        )
        return FullResult(success=True, error="", compile=comp, run=run)
    else:
        raise ValueError(f"Invalid language {config['lang']}")
