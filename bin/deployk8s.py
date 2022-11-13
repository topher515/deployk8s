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


ENV_NAMES = ["dev", "prod"]
DEFAULT_ENV = "dev"


# TODO: Change this
APP_NAME = os.getenv('DEPLOYER_APP_PREFIX')


GENERIC_SECRET_FIELD_NAME = "value"


class MntSecretFileMeta(TypedDict):
    filename: str
    local_path: str

VOL_MNT_WHITELIST = '-' + string.ascii_lowercase + string.digits


# UTILS

def exec(*args):
    print(f"{bcolors.DEMPH}Running command: {args}{bcolors.ENDC}")
    return check_call(args)


def exec_io(*args, **kwargs):
    print(f"{bcolors.DEMPH}Running io command: {args}{bcolors.ENDC}")
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


def make_release_name(env: str):
    return f"{APP_NAME}-{env}"


def write_wiz_config(dirpath: Path, config: dict):
    wizdir = dirpath / 'wiz'
    config_yml = wizdir / 'wiz.yml'
    with open(config_yml, 'w') as fp:
        yaml.dump(config, fp)


def load_wiz_config(dirpath: Path, key: Optional[str] = None):
    wizdir = dirpath / 'wiz'
    config_yml = wizdir / 'wiz.yml'

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


# CLI

@click.group()
def cli():
    if not APP_NAME:
        raise RuntimeError("You must configure deployer APP_NAME")


@cli.command("info")
def sync_cmd():

    print(exec_io('kubectl', 'config', 'current-context').decode('utf8'))
    print(f"APP_NAME={APP_NAME}")


def _set_docker_registry_secret(namespace, secret_name, email, username, password):
    exec(
        "kubectl",
        f"--namespace={namespace}",
        "create",
        "secret",
        "docker-registry",
        secret_name,
        "--docker-server=ghcr.io",
        f"--docker-username={username}",
        f"--docker-password={password}",
        f"--docker-email={email}"
    )


@cli.group("release")
def release_cli():
    """
    Manipulate k8s releases
    """


@release_cli.command('nuke')
@click.argument("dirpath")
def nuke_cmd(dirpath):
    dirpath = Path(dirpath)
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    exec(
        "helm",
        "uninstall",
        f"--namespace={namespace}",
        make_release_name(env)
    )


@release_cli.command('list')
@click.argument("dirpath")
def list_cmd(dirpath):
    dirpath = Path(dirpath)
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    exec(
        "helm",
        "history",
        f"--namespace={namespace}",
        make_release_name(env)
    )


@release_cli.command('rollback')
@click.argument("dirpath")
@click.argument("revision")
def rollback_cmd(dirpath, revision):
    dirpath = Path(dirpath)
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    exec(
        "helm",
        "rollback",
        f"--namespace={namespace}",
        make_release_name(env),
        revision
    )


@cli.group("secret")
def secret_cli():
    """
    Manipulate normal k8s secrets
    """


@secret_cli.command('list')
@click.argument("dirpath")
def list_cmd(dirpath):
    namespace = load_namespace_from_config(Path(dirpath))
    exec(
        'kubectl',
        'get',
        'secret',
        f'--namespace={namespace}'
    )


@secret_cli.command('set')
@click.argument("dirpath")
@click.argument("secret_name")
@click.argument("secret_value")
def set_secret_cmd(dirpath, secret_name, secret_value):
    namespace = load_namespace_from_config(Path(dirpath))
    _set_secret_cmd(namespace, secret_name, secret_value)


@secret_cli.command('get')
@click.option('--no-parse', is_flag=True)
@click.argument("dirpath")
@click.argument("secret_name")
def get_secret_cmd(dirpath, no_parse, secret_name):
    namespace = load_namespace_from_config(Path(dirpath))
    extras = [
        "-o=jsonpath='{.data}'"] if no_parse else ["-o=jsonpath='{.data.value}'"]
    output = exec_io(
        "kubectl",
        "get",
        "secret",
        f'--namespace={namespace}',
        secret_name,
        # This also comes from GENERIC_SECRET_FIELD_NAME
        *extras)
    if no_parse:
        print(output.decode('utf8'))
    else:
        print(b64decode(output).decode('utf8'))


@secret_cli.command('rm')
@click.argument("dirpath")
@click.argument("secret_name")
def rm_secret_cmd(dirpath, secret_name):
    namespace = load_namespace_from_config(Path(dirpath))
    exec(
        "kubectl",
        "remove",
        "secret",
        f'--namespace={namespace}',
        secret_name
    )


@cli.group("envsecret")
def envsecret_cli():
    """
    Manipulate k8s secrets for use with ENV_VARs
    """


@envsecret_cli.command('set')
@click.argument("dirpath")
@click.argument("envvar_name")
@click.argument("envvar_value")
def set_envvar_cmd(dirpath, envvar_name, envvar_value):
    dirpath = Path(dirpath)
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    secret_name = make_envsecret_name(env, envvar_name)
    _set_secret_cmd(namespace, secret_name, envvar_value)


def _push_envfile(namespace, env, dotenv_file):

    dotenv_vals: Dict[str, str] = dotenv_values(dotenv_file)

    for envvar_name, envvar_value in dotenv_vals.items():
        secret_name = make_envsecret_name(env, envvar_name)
        _set_secret_cmd(namespace, secret_name, envvar_value)


@envsecret_cli.command('pushfile')
@click.argument("dirpath")
@click.argument("dotenv_file")
def set_envvar_cmd(dirpath, dotenv_file):
    dirpath = Path(dirpath)
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    return _push_envfile(namespace, env, dotenv_file)


@cli.group("mntsecret")
def mntsecret_cli():
    """
    Manipulate k8s secrets for use with mounted secret file volumes
    """


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


@mntsecret_cli.command("set")
@click.argument("dirpath")
@click.argument("remote_filepath")
@click.argument("local_filepath")
def set_mntsecret_cli(dirpath, remote_filepath, local_filepath):
    dirpath = Path(dirpath)
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)
    _set_file_as_secret(namespace, env, remote_filepath, local_filepath)


@cli.group("wiz")
def wiz_cli():
    """
    Wizard for pushing secrets / releasing from a env var dir
    """


def _iter_filepaths(dirpath: Path):

    for local_path in dirpath.rglob('*'):
        if local_path.is_dir():
            continue
        remote_path = str(local_path).split(str(dirpath))[1]
        yield local_path, remote_path


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


@wiz_cli.command("setup")
@click.argument("dirpath")
def wiz_setup(dirpath):

    dirpath = Path(dirpath)
    wizdir = dirpath / 'wiz'

    config = load_wiz_config(dirpath)

    print("Ensuring namespace is setup", file=sys.stderr)
    namespace = load_namespace_from_config(dirpath)
    if not _is_extant_k8s_item("namespace", namespace):
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
        email = click.prompt("- Email?")
        username = click.prompt("- Username?")
        password = click.prompt("- Password?")
        _set_docker_registry_secret(
            namespace, image_pull_secret_name, email, username, password)


@wiz_cli.command("push")
@click.argument("dirpath")
def wiz_push(dirpath):
    '''
    Push the secrets data derived from the wiz env dir
    '''
    dirpath = Path(dirpath)
    env = load_wiz_config(dirpath, "envName")
    namespace = load_namespace_from_config(dirpath)

    # Handle push .env file
    wizdir = dirpath / "wiz"
    dotenv_file = wizdir / '.env'
    _push_envfile(namespace, env, str(dotenv_file))

    # Handle secret files for mnting
    for remote_dir, file_metas in _get_file_metas(wizdir / 'secretfiles').items():
        _set_files_as_secret(namespace, env, remote_dir, file_metas)


def _wiz_genvalues(dirpath):
    '''
    Print the `values.yaml` file generated from this wiz env dir
    '''

    dirpath = Path(dirpath)
    wizdir = dirpath / 'wiz'

    env = load_wiz_config(dirpath, "envName")
    image_pull_secret_name = load_wiz_config(dirpath, "imagePullSecret")

    dotenv_file = wizdir / '.env'
    dotenv_vals: Dict[str, str] = dotenv_values(dotenv_file)

    values = {}
    envs = [make_envsecret(env, env_name) for env_name in dotenv_vals.keys()]

    vols = []
    vol_mnts = []

    for remote_dir, file_metas in _get_file_metas(wizdir / 'secretfiles').items():
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


@wiz_cli.command("genvalues")
@click.argument("dirpath")
def wiz_genvalues(dirpath):
    '''
    Print the values.yml generated from the wiz env dir
    '''
    values = _wiz_genvalues(dirpath)
    yaml.dump(values, sys.stdout)


@wiz_cli.command("release")
@click.argument("dirpath")
@click.argument("image")
def wiz_release(dirpath, image):
    '''
    Create a helm release from values.yml generated from the wiz env dir
    (and the helm chart which must be in the parent directory from the wiz env dir)
    '''
    values = _wiz_genvalues(dirpath)

    dirpath = Path(dirpath)

    namespace = load_namespace_from_config(dirpath)
    env = load_wiz_config(dirpath, "envName")

    helm_chart_dir = str((dirpath / '..').resolve())

    print(f'{bcolors.OKCYAN}Deploying image="{image}" to env="{env}"...\n{bcolors.ENDC}')
    exec(
        "helm", "dependency", "update", helm_chart_dir
    )

    with NamedTemporaryFile('w') as values_file:
        json.dump(values, values_file)
        values_file.flush()
        exec(
            "helm",
            "upgrade",  # Perform install or upgrade
            "--create-namespace",  # Create namespace if it doesnt exist
            f"--namespace={namespace}",
            "--install", make_release_name(env),
            helm_chart_dir,
            "--set", f"image={image}",
            f"--values={values_file.name}",
        )


cli.add_command(wiz_cli)
cli.add_command(release_cli)
cli.add_command(secret_cli)
cli.add_command(envsecret_cli)


if __name__ == "__main__":
    cli()
