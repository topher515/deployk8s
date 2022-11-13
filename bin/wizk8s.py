#!/usr/bin/env python3

from functools import cache
import json
import os
import random
import string
import subprocess
import sys
from base64 import b64decode
from collections import defaultdict
from pathlib import Path
from subprocess import CalledProcessError, check_call
from tempfile import NamedTemporaryFile
from typing import Dict, List, Optional, TypedDict

import click
import click.exceptions
import yaml
from dotenv import dotenv_values

GLOBALS = {
    "verbose": False
}

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    DEMPH = '\033[1m'
    UNDERLINE = '\033[4m'

GENERIC_SECRET_FIELD_NAME = "value"

class MntSecretFileMeta(TypedDict):
    filename: str
    local_path: str

VOL_MNT_WHITELIST = '-' + string.ascii_lowercase + string.digits


# UTILS

def verbose_print(*args, **kwargs):
    if GLOBALS["verbose"]:
        print(*args, **kwargs)

def exec(*args):
    verbose_print(f"Running command: {args}")
    return check_call(args)


def exec_io(*args, **kwargs):
    verbose_print(f"Running io command: {args}")
    proc = subprocess.run(args, capture_output=True, timeout=30, **kwargs)
    try:
        proc.check_returncode()
    except CalledProcessError:
        print(proc.stderr, file=sys.stderr)
    return proc.stdout


def _is_extant_k8s_item(item_type: str, item_name: str):
    proc = subprocess.run([
        'kubectl', 'get', item_type, item_name
    ], capture_output=True, timeout=30)
    if proc.returncode == 0:
        return True
    if 'Error from server (NotFound)' in proc.stderr.decode('utf8'):
        return False
    proc.check_returncode()  # Something else broke


def _is_extant_secret(secret_name) -> bool:
    return _is_extant_k8s_item('secret', secret_name)


def make_release_name(chart_name: str, env: str):
    return f"{chart_name}-{env}"


def write_wiz_config(dirpath: Path, config: dict):
    config_yml = dirpath / 'wiz.yaml'
    with open(config_yml, 'w') as fp:
        yaml.dump(config, fp)


def load_wiz_config(dirpath: Path, key: Optional[str] = None):
    config_yml = dirpath / 'wiz.yaml'

    try:
        with open(config_yml, 'r') as fp:
            config = yaml.safe_load(fp)
    except FileNotFoundError:
        config = {}

    if not key:
        return config

    if config.get(key) is None:
        raise click.exceptions.UsageError(
            f"{config_yml} does not have {key} set")

    return config[key]


def load_wiz_config_key_or_prompt(dirpath: Path, key: str):
    try:
        return load_wiz_config(dirpath, key)
    except click.exceptions.UsageError:
        val = click.prompt(f"Missing config key {key}. Set it?")
        if not val:
            raise
        config = load_wiz_config(dirpath)
        config[key] = val
        write_wiz_config(dirpath, config)
        return val

@cache
def load_namespace_from_config(dirpath: Path):
    return load_wiz_config_key_or_prompt(dirpath, 'namespace')


def make_envsecret_name(env: str, env_var_name: str):
    env_var_slug = env_var_name.lower().replace('_', '-')
    return f"envsecret-{env}-{env_var_slug}"


def make_envsecret(env: str, env_var_name: str):
    return {
        "name": env_var_name,
        "valueFrom": {
            "secretKeyRef": {
                "key": GENERIC_SECRET_FIELD_NAME,
                "name": make_envsecret_name(env, env_var_name)
            }
        }
    }



def make_mntsecret_name(env: str, filepath: str):
    slug = ''.join(
        x if x in VOL_MNT_WHITELIST else '-' for x in filepath.lower())
    return f"mntsecret-{env}-{slug}"


def make_mntsecret_volume_data(env: str, mntdir: str):
    name = make_mntsecret_name(env, mntdir)
    vol = {
        "name": name,
        "secret": {
            "secretName": name
        }
    }
    vol_mnt = {
        "mountPath": mntdir,
        "name": name,
        "readOnly": True
    }
    return vol, vol_mnt


def _set_secret_multi_cmd(namespace, secret_name: str, secrets: Dict[str, str]):

    print(f"{bcolors.OKBLUE}Will save secret '{secret_name}'{bcolors.ENDC}")
    from_literals = [
        f'--from-literal={secret_key}={secret_value}'
        for secret_key, secret_value in secrets.items()
    ]

    out = exec_io(
        'kubectl',
        'create',
        'secret',
        f'--namespace={namespace}',
        '--dry-run=client',
        'generic',
        '-o',
        'yaml',
        secret_name,
        *from_literals,
    )
    exec_io(
        'kubectl',
        'apply',
        '-f',
        '-',
        input=out
    )


def _set_secret_cmd(namespace, secret_name: str, secret_value: str):
    return _set_secret_multi_cmd(namespace, secret_name, {"value": secret_value})


def _is_extant_secret(secret_name) -> bool:

    proc = subprocess.run([
        'kubectl', 'get', 'secret', secret_name
    ], capture_output=True, timeout=30)
    if proc.returncode == 0:
        return True
    if 'Error from server (NotFound)' in proc.stderr.decode('utf8'):
        return False
    proc.check_returncode()  # Something else broke


def _get_helm_chart_dir(dirpath: Path):
    dirpath = dirpath / '..'
    while not (dirpath / 'Chart.yaml').is_file():
        parentdir = dirpath / ".."
        if dirpath.resolve() == parentdir.resolve():
            # We've reached the root
            return None
        dirpath = parentdir
    return dirpath.resolve()

def _get_helm_chart_name(dirpath: Path):
    helm_chart_dir= _get_helm_chart_dir(dirpath)
    with open(helm_chart_dir / "Chart.yaml") as fp:
        chart = yaml.safe_load(fp)
    return chart["name"]

def _release_create(image: str):
    '''
    Create a helm release from values.yml generated from the wiz env dir
    (and the helm chart which must be in the parent directory from the wiz env dir)
    '''
    dirpath = Path(GLOBALS["dirpath"])
    values = _wiz_genvalues(dirpath)

    namespace = load_namespace_from_config(dirpath)
    env = load_wiz_config(dirpath, "envName")

    helm_chart_dir = _get_helm_chart_dir(dirpath)
    chart_name = _get_helm_chart_name(dirpath)
    release_name = make_release_name(chart_name, env)

    print(f'{bcolors.OKCYAN}Deploying image="{image}" as release="{release_name}"\n{bcolors.ENDC}')
    exec(
        "helm", "dependency", "update", str(helm_chart_dir)
    )

    with NamedTemporaryFile('w') as values_file:
        json.dump(values, values_file)
        values_file.flush()
        exec(
            "helm",
            "upgrade",  # Perform install or upgrade
            "--create-namespace",  # Create namespace if it doesnt exist
            f"--namespace={namespace}",
            "--install", release_name,
            str(helm_chart_dir),
            "--set", f"image={image}",
            f"--values={values_file.name}",
        )


# CLI

@click.group()
@click.option("--verbose", is_flag=True)
@click.option("--dirpath", help="The path to the wiz env dir")
def cli(verbose, dirpath):
    GLOBALS["verbose"] = verbose

    if not dirpath:
        cwd = os.getcwd()
        helm_chart_dir = _get_helm_chart_dir(Path(cwd))
        if helm_chart_dir:
            # Has ancestor helm chart dir, so its ok to use
            print(f"Using current dir '{cwd}' as --dirpath", file=sys.stderr)
            dirpath = cwd
        else:
            raise click.exceptions.UsageError("You must specify --dirpath (or cd to the wiz env dir)")
    
    GLOBALS["dirpath"] = dirpath


@cli.command("info")
def sync_cmd():
    """
    Get info about this wiz dir env (and other context)
    """
    dirpath = Path(GLOBALS["dirpath"])
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    chart_name = _get_helm_chart_name(dirpath)
    cluster_name = exec_io('kubectl', 'config', 'current-context').decode('utf8').strip()
    
    print(f"cluster: {cluster_name}")
    print(f"namespace: {namespace}")
    print(f"release_name: {make_release_name(chart_name, env)}")


def _set_docker_registry_secret(namespace, hostname, secret_name, email, username, password):
    exec(
        "kubectl",
        f"--namespace={namespace}",
        "create",
        "secret",
        "docker-registry",
        secret_name,
        "--docker-server={hostname}",
        f"--docker-username={username}",
        f"--docker-password={password}",
        f"--docker-email={email}"
    )


@cli.group("releases")
def release_cli():
    """
    Manipulate k8s releases
    """


@release_cli.command('nuke')
def nuke_cmd():
    dirpath = Path(GLOBALS["dirpath"])
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    chart_name = _get_helm_chart_name(dirpath)
    exec(
        "helm",
        "uninstall",
        f"--namespace={namespace}",
        make_release_name(chart_name, env)
    )


@release_cli.command('list')
def list_cmd():
    dirpath = Path(GLOBALS["dirpath"])
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    chart_name = _get_helm_chart_name(dirpath)
    exec(
        "helm",
        "history",
        f"--namespace={namespace}",
        make_release_name(chart_name, env)
    )


@release_cli.command('rollback')
@click.argument("revision")
def rollback_cmd(revision):
    dirpath = Path(GLOBALS["dirpath"])
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    chart_name = _get_helm_chart_name(dirpath)
    exec(
        "helm",
        "rollback",
        f"--namespace={namespace}",
        make_release_name(chart_name, env),
        revision
    )


@cli.group("secrets")
def secret_cli():
    """
    Manipulate normal k8s secrets
    """


@secret_cli.command('list')
def list_cmd():
    namespace = load_namespace_from_config(Path(GLOBALS["dirpath"]))
    exec(
        'kubectl',
        'get',
        'secret',
        f'--namespace={namespace}'
    )


@secret_cli.command('set')
@click.argument("secret_name")
@click.argument("secret_value")
def set_secret_cmd(secret_name, secret_value):
    namespace = load_namespace_from_config(Path(GLOBALS["dirpath"]))
    _set_secret_cmd(namespace, secret_name, secret_value)


@secret_cli.command('get')
@click.option('--no-parse', is_flag=True)
@click.argument("secret_name")
def get_secret_cmd(no_parse, secret_name):
    namespace = load_namespace_from_config(Path(GLOBALS["dirpath"]))
    extras = [
        # data.value must match the GENERIC_SECRET_FIELD_NAME!
        "-o=jsonpath='{.data}'"] if no_parse else ["-o=jsonpath='{.data.value}'"]
    output = exec_io(
        "kubectl",
        "get",
        "secret",
        f'--namespace={namespace}',
        secret_name,
        *extras)
    if no_parse:
        print(output.decode('utf8'))
    else:
        print(b64decode(output).decode('utf8'))


@secret_cli.command('rm')
@click.argument("secret_name")
def rm_secret_cmd(secret_name):
    namespace = load_namespace_from_config(Path(GLOBALS["dirpath"]))
    exec(
        "kubectl",
        "delete",
        "secret",
        f'--namespace={namespace}',
        secret_name
    )


@secret_cli.command('set-as-envar')
@click.argument("envvar_name")
@click.argument("envvar_value")
def set_envvar_cmd(envvar_name, envvar_value):
    """
    Set the ENV_VAR as a secret
    """
    dirpath = Path(GLOBALS["dirpath"])
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    secret_name = make_envsecret_name(env, envvar_name)
    _set_secret_cmd(namespace, secret_name, envvar_value)


def _push_envfile(namespace, env, dotenv_file):

    dotenv_vals: Dict[str, str] = dotenv_values(dotenv_file)

    for envvar_name, envvar_value in dotenv_vals.items():
        secret_name = make_envsecret_name(env, envvar_name)
        _set_secret_cmd(namespace, secret_name, envvar_value)


@secret_cli.command('set-from-env-file')
@click.argument("dotenv_file")
def set_envvar_cmd(dotenv_file):
    """
    Set all the ENV_VAR values in the given files as secrets
    """
    dirpath = Path(GLOBALS["dirpath"])
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    return _push_envfile(namespace, env, dotenv_file)


def _set_files_as_secret(namespace, env, remote_dir, file_metas: List[MntSecretFileMeta]):

    if not remote_dir:
        raise RuntimeError(
            "You must specify a full remote path, not just a filename")

    secret_name = make_mntsecret_name(env, remote_dir)
    secret_contents = {}

    for filemeta in file_metas:
        local_filepath = filemeta["local_path"]
        with open(local_filepath, 'r') as fp:
            contents = fp.read()
        secret_contents[filemeta["filename"]] = contents
        print(
            f"{bcolors.OKBLUE}Will make local file '{local_filepath}' available in dir '{remote_dir}' as '{filemeta['filename']}'{bcolors.ENDC}")

    _set_secret_multi_cmd(namespace, secret_name, secret_contents)


def _set_file_as_secret(namespace, env, remote_filepath, local_filepath):

    dirname = os.path.dirname(remote_filepath)
    basename = os.path.basename(remote_filepath)
    _set_files_as_secret(namespace, 
        env, dirname, [{"filename": basename, "local_path": local_filepath}])


@secret_cli.command("set")
@click.argument("local_filepath")
@click.argument("remote_filepath")
def set_mntsecret(local_filepath, remote_filepath):
    """
    Save contents of local file as a volume-mountable-secret 
    """
    dirpath = Path(GLOBALS["dirpath"])
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    _set_file_as_secret(namespace, env, remote_filepath, local_filepath)


def _get_file_metas(dirpath: Path) -> Dict[str, List[MntSecretFileMeta]]:

    dir_bucket = defaultdict(list)

    for local_path in dirpath.rglob('*'):
        if local_path.is_dir():
            continue

        remote_path = Path(str(local_path).split(str(dirpath))[1])
        dir_bucket[str(remote_path.parent)].append(MntSecretFileMeta(
            filename=local_path.name,
            local_path=str(local_path),
        ))
    return dict(dir_bucket)


@cli.command("setup")
def wiz_setup():
    """
    Do initial setup for config
    """

    dirpath = Path(GLOBALS["dirpath"])

    config = load_wiz_config(dirpath)

    print("Ensuring namespace is setup", file=sys.stderr)
    namespace = load_namespace_from_config(dirpath)
    if not _is_extant_k8s_item("namespace", namespace):
        print("Creating namespace", file=sys.stderr)
        exec("kubectl", "create", "namespace", namespace)

    # Handle env name
    print("Ensuring envName is setup", file=sys.stderr)
    env = config.get("envName")
    if not env:
        print("The wiz dir does not have an envName configured.")
        env = click.prompt("- env name? (e.g., dev, prod)")
        config["envName"] = env
        write_wiz_config(dirpath, config)

    # Handle image pull secret config
    print("Ensuring imagePullSecret is setup", file=sys.stderr)
    image_pull_secret_name = config.get('imagePullSecret')
    if not image_pull_secret_name:
        randstr = ''.join(random.choices(
            string.ascii_lowercase + string.digits, k=5))
        image_pull_secret_name = f'wiz-setup-imagepullsecret-{env}-{randstr}'
        config['imagePullSecret'] = image_pull_secret_name
        write_wiz_config(dirpath, config)

    if not _is_extant_secret(image_pull_secret_name):
        print(
            f"Docker Registry Secret '{image_pull_secret_name}' does not exist. Creating it...")
        print("(For Github password user a Personal Access Token: https://github.com/settings/tokens)")
        hostname = click.prompt("- Hostname?", default="ghcr.io")
        email = click.prompt("- Email?")
        username = click.prompt("- Username?")
        password = click.prompt("- Password?")
        _set_docker_registry_secret(
            namespace, hostname, image_pull_secret_name, email, username, password)


@cli.command("push")
def wiz_push():
    '''
    Push the current config
    '''
    dirpath = Path(GLOBALS["dirpath"])
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)

    # Handle push .env file
    dotenv_file = dirpath / '.env'
    _push_envfile(namespace, env, str(dotenv_file))

    # Handle secret files for mnting
    for remote_dir, file_metas in _get_file_metas(dirpath / 'secretfiles').items():
        _set_files_as_secret(namespace, env, remote_dir, file_metas)


def _wiz_genvalues(dirpath: str):
    dirpath = Path(dirpath)

    env = load_wiz_config(dirpath, "envName")
    image_pull_secret_name = load_wiz_config(dirpath, "imagePullSecret")

    dotenv_file = dirpath / '.env'
    dotenv_vals: Dict[str, str] = dotenv_values(dotenv_file)

    values = {}
    envs = [make_envsecret(env, env_name) for env_name in dotenv_vals.keys()]

    vols = []
    vol_mnts = []

    for remote_dir, file_metas in _get_file_metas(dirpath / 'secretfiles').items():
        vol, vol_mnt = make_mntsecret_volume_data(env, remote_dir)
        vols.append(vol)
        vol_mnts.append(vol_mnt)

    values = {
        "env": envs,
        "volumes": vols,
        "volumeMounts": vol_mnts,
        "imagePullSecrets": [{"name": image_pull_secret_name}]
    }

    return values


@cli.command("genvalues")
def wiz_genvalues():
    '''
    Print the values.yml generated from current config
    '''
    values = _wiz_genvalues(GLOBALS["dirpath"])
    yaml.dump(values, sys.stdout)


@cli.command("deploy")
@click.argument("image")
def wiz_deploy(image):
    """
    Create a new release
    """
    _release_create(image)


cli.add_command(release_cli)
cli.add_command(secret_cli)


if __name__ == "__main__":
    cli()
