import concurrent.futures
import contextlib
import functools
import glob
import itertools
import os
import pickle
import shutil
import threading

from multiprocessing import cpu_count

import click
import sh
import toolz
import yaml
import requests

import tqdm
import decorating

import conda.api

from conda_build.metadata import MetaData

from conda.resolve import MatchSpec

from conda_build_all.version_matrix import special_case_version_matrix


@contextlib.contextmanager
def pool(sequence, klass, max_workers=None):
    if max_workers is None:
        # Use at most half the number of CPUs available
        max_workers = max(1, min(cpu_count() // 2, len(sequence)))
    with klass(max_workers=max_workers) as e:
        yield e


@contextlib.contextmanager
def thread_pool(sequence, max_workers=None):
    with pool(
        sequence,
        concurrent.futures.ThreadPoolExecutor,
        max_workers=max_workers,
    ) as e:
        yield e


@contextlib.contextmanager
def process_pool(sequence, max_workers=None):
    with pool(
        sequence,
        concurrent.futures.ProcessPoolExecutor,
        max_workers=max_workers,
    ) as e:
        yield e


@click.group(context_settings=dict(help_option_names=['-h', '--help']))
def cli():
    pass


def parse_git_version_spec(ctx, param, packages):
    package_specifications = {}
    for package in packages:
        package, *version = package.split('@', 1)
        package_specifications[package] = version[0] if version else None
    return package_specifications


def modify_metadata(meta, version, deps_to_build):
    if version is None:
        return meta

    meta.meta['package']['version'] = "{{ environ.get('GIT_DESCRIBE_TAG', '').replace('v', '') }}"  # noqa: E501
    meta.meta['build']['number'] = "{{ environ.get('GIT_DESCRIBE_NUMBER', 0) }}"  # noqa: E501
    meta.meta['source'] = {
        'git_url': meta.get_value('about/home'),
        'git_rev': version,
    }
    requirements = meta.meta['requirements']
    build = requirements['build']
    run = requirements['run']

    if deps_to_build:
        for i, dep in enumerate(build):
            name, *_ = dep.split()
            if name in deps_to_build:
                meta.meta['requirements']['build'][i] = name

        for i, dep in enumerate(run):
            name, *_ = dep.split()
            if name in deps_to_build:
                meta.meta['requirements']['run'][i] = name

    return meta


def construct_dependency_subgraph(metadata):
    graph = {package: set() for package in metadata.keys()}

    for package, meta in metadata.items():
        requirements = frozenset(
            meta.get_value('requirements/build') +
            meta.get_value('requirements/run')
        )
        for dep in requirements:
            dep_name, *_ = dep.split()
            if dep_name in graph and dep_name != package:
                graph[package].add(dep_name)
    return graph


@cli.command(help='Get the sha of a GitHub repo ref without using git locally')
@click.argument('repo')
@click.argument('ref')
def sha(repo, ref):
    uri = 'https://api.github.com/repos/{}/git/refs/heads/{}'.format(repo, ref)
    req = requests.get(uri)
    js = req.json()
    click.echo(js['object']['sha'])


@cli.command(help='Pull in the conda forge docker image and clone feedstocks.')
@click.argument(
    'package_specifications', callback=parse_git_version_spec, nargs=-1)
@click.option(
    '-i',
    '--image', required=False, default='condaforge/linux-anvil',
    help='The Docker image to use to build packages.'
)
@click.option(
    '-a', '--artifact-directory',
    default=os.path.join('.', 'artifacts'),
    type=click.Path(),
    help='The location to place tarballs upon a successful build.',
)
def init(package_specifications, image, artifact_directory):
    try:
        sh.docker.pull(image, _out=click.get_binary_stream('stdout'))
    except sh.ErrorReturnCode as e:
        click.get_binary_stream('stderr').write(e.stderr)
        raise SystemExit(e.exit_code)

    feedstocks = [
        'https://github.com/conda-forge/{}'.format(feedstock)
        for feedstock in map('{}-feedstock'.format, package_specifications)
        if not os.path.exists(feedstock)
    ]

    if feedstocks:
        progress = functools.partial(
            tqdm.tqdm,
            total=len(feedstocks),
            desc='Cloning feedstocks'
        )
    else:
        progress = iter

    with thread_pool(package_specifications) as executor:
        futures = [
            executor.submit(sh.git.clone, feedstock)
            for feedstock in feedstocks
        ]

        for future in progress(concurrent.futures.as_completed(futures)):
            try:
                future.result()
            except sh.ErrorReturnCode as e:
                click.get_binary_stream('stderr').write(e.stderr)
                raise SystemExit(e.exit_code)

    packages = tuple(package_specifications.keys())

    recipes_directory = os.path.join('.', 'recipes')
    shutil.rmtree(recipes_directory, ignore_errors=True)

    build_artifacts = os.path.join(artifact_directory, 'build_artifacts')

    os.makedirs(recipes_directory, exist_ok=True)
    os.makedirs(artifact_directory, exist_ok=True)
    os.makedirs(build_artifacts, exist_ok=True)

    recipe_paths = []

    for package in packages:
        package_recipe_directory = os.path.join(recipes_directory, package)
        recipe_path = os.path.join('{}-feedstock'.format(package), 'recipe')
        shutil.copytree(recipe_path, package_recipe_directory)
        recipe_paths.append(recipe_path)

    with process_pool(packages) as executor:
        package_metadata = dict(
            zip(packages, executor.map(MetaData, recipe_paths))
        )

    graph = construct_dependency_subgraph(package_metadata)

    metadata = {
        package: modify_metadata(meta, version, graph[package])
        for meta, (package, version) in zip(
            package_metadata.values(), package_specifications.items()
        )
    }

    for package, meta in metadata.items():
        path = os.path.join(recipes_directory, package, 'meta.yaml')
        with open(path, mode='wt') as f:
            yaml.dump(meta.meta, f, default_flow_style=False)

    with open('.artifactdir', mode='wt') as f:
        f.write(os.path.abspath(artifact_directory))

    with open('.recipedir', mode='wt') as f:
        f.write(os.path.abspath(recipes_directory))

    with open('.metadata', mode='wb') as f:
        pickle.dump(package_metadata, f)


def tarballs(build_artifacts):
    pattern = os.path.join(build_artifacts, 'linux-64', '*.tar.bz2')
    return [
        tarball for tarball in glob.glob(pattern)
        if not os.path.basename(tarball).startswith('repodata')
    ]


@cli.command(help='Remove feedstocks and generated files')
def clean():
    if os.path.exists('.artifactdir'):
        with open('.artifactdir', mode='rt') as f:
            artifact_directory = f.read().strip()

        build_artifacts = os.path.join(artifact_directory, 'build_artifacts')

        if glob.glob(os.path.join(build_artifacts, '*')):
            try:
                sh.docker.run(
                    '-a', 'stdin',
                    '-a', 'stdout',
                    '-a', 'stderr',
                    '-v', '{}:/build_artifacts'.format(build_artifacts),
                    '-i',
                    '--rm',
                    '-e', 'HOST_USER_ID={:d}'.format(os.getuid()),
                    'condaforge/linux-anvil',
                    'bash',
                    _in='rm -rf /build_artifacts/*',
                )
            except sh.ErrorReturnCode as e:
                click.get_binary_stream('stderr').write(e.stderr)

    files = [path for path in os.listdir(path='.')]

    if files:
        with thread_pool(files) as executor:
            futures = [
                executor.submit(
                    functools.partial(shutil.rmtree, ignore_errors=True)
                    if os.path.isdir(path) else os.remove,
                    path
                ) for path in files
            ]

            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    click.ClickException(str(e))


SCRIPT = '\n'.join([
    'conda clean --lock',
    'conda install --yes --quiet conda-forge-build-setup',
    'source run_conda_forge_build_setup',
    (
        'CONDA_PY={python} CONDA_NPY={numpy} '
        'conda build {use_local} /recipes/{package} '
        '--output-folder /build_artifacts --quiet || exit 1'
    ),
])


def update_animation(
    animation,
    formatter,
    built,
    future,
    lock=threading.Lock(),
):
    with lock:
        animation.spinner.message = formatter(next(built))


def empty_padding(*args, **kwargs):
    """Don't show the default sine wave when animating."""
    return ''


animated = functools.partial(decorating.animated, fpadding=empty_padding)


def construct_environment_variables(ctx, param, environment):
    result = {}
    for package, var in (x.split(':', 1) for x in environment):
        result.setdefault(package, set()).add(var)
    return result


@cli.command(help='Build conda forge packages in parallel')
@click.option(
    '-c', '--constraints',
    multiple=True,
    help='Additional special software constraints - e.g., numpy/python.'
)
@click.option(
    '-e', '--environment',
    multiple=True,
    callback=construct_environment_variables,
    help='Additional environment variables to pass to builds',
)
@click.option(
    '-L', '--use-local', is_flag=True,
    help=(
        'Use builds that exist locally if possible. '
        'Note that this may slow down build times because packages must be '
        'built in topological order with this option set.'
    )
)
@click.option(
    '-j', '--jobs',
    type=int,
    default=max(1, cpu_count() // 2),
    help='Number of workers to use for building',
)
def build(constraints, use_local, jobs, environment):
    with open('.metadata', mode='rb') as f:
        metadata = pickle.load(f)

    if not environment.keys() <= metadata.keys():
        missing_packages = environment.keys() - metadata.keys()
        raise click.ClickException(
            'Environment variables defined for missing packages {}'.format(
                set(missing_packages)
            )
        )

    if not constraints:
        constraints = 'python >=2.7,<3|>=3.4', 'numpy >=1.10', 'r-base >=3.3.2'

    constraint_specifications = {
        key.name: value for key, value in zip(
            *itertools.tee(map(MatchSpec, constraints))
        )
    }

    raw_index = conda.api.get_index(
        ('defaults', 'conda-forge'),
        platform='linux-64',
    )

    index = {
        dist: record for dist, record in raw_index.items()
        if dist.name not in constraint_specifications or (
            dist.name in constraint_specifications and
            constraint_specifications[dist.name].match(dist)
        )
    }

    get_version_matrix = toolz.flip(special_case_version_matrix)(index)

    with open('.recipedir', mode='rt') as f:
        recipes_directory = f.read().strip()

    recipes = glob.glob(os.path.join(recipes_directory, '*'))

    if not recipes:
        raise click.ClickException(
            'No recipes found in {}'.format(recipes_directory)
        )

    with open('.artifactdir', mode='rt') as f:
        artifact_directory = f.read().strip()

    build_artifacts = os.path.join(artifact_directory, 'build_artifacts')

    with animated('Constraining special versions (e.g., numpy and python)'):
        with process_pool(metadata) as executor:
            results = executor.map(get_version_matrix, metadata.values())

    matrices = dict(zip(metadata.keys(), results))

    scripts = {
        (
            package,
            constraints.get('python', '').replace('.', ''),
            constraints.get('numpy', '').replace('.', ''),
        ): SCRIPT.format(
            package=package,
            python=constraints.get('python', '').replace('.', ''),
            numpy=constraints.get('numpy', '').replace('.', ''),
            use_local='--use-local' if use_local else '',
        )
        for package, matrix in matrices.items()
        for constraints in map(dict, filter(None, matrix))
    }

    os.makedirs('logs', exist_ok=True)
    for text_file in glob.glob(os.path.join('logs', '*.txt')):
        os.remove(text_file)

    args = (
        '-a', 'stdin',
        '-a', 'stdout',
        '-a', 'stderr',
        '-v', '{}:/build_artifacts'.format(
            os.path.abspath(build_artifacts)
        ),
        '-v', '{}:/recipes'.format(os.path.abspath(recipes_directory)),
        '--dns', '8.8.8.8',
        '--dns', '8.8.4.4',
        '-e', 'HOST_USER_ID={:d}'.format(os.getuid()),
    )

    tasks = [
        sh.docker.run.bake(
            *functools.reduce(
                lambda args, var: args + ('-e', var),
                environment.get(package, ()),
                args,
            ),
            interactive=True,
            rm=True,
            _in=script,
            _out=os.path.join(
                'logs', '{package}-py{python}-np{numpy}.txt'.format(
                    package=package,
                    python=python or '_none',
                    numpy=numpy or '_none',
                )
            ),
        ) for (package, python, numpy), script in scripts.items()
    ]

    ntasks = len(tasks)
    built = itertools.count()
    first = next(built)
    format_string = 'Built {{:{padding}d}}/{:{padding}d} packages'
    formatter = format_string.format(ntasks, padding=len(str(ntasks))).format
    animation = animated(formatter(first))
    update_when_done = functools.partial(
        update_animation,
        animation,
        formatter,
        built,
    )
    futures = []

    with thread_pool(tasks, max_workers=jobs) as executor:
        for task in tasks:
            future = executor.submit(task, 'condaforge/linux-anvil', 'bash')
            future.add_done_callback(update_when_done)
            futures.append(future)

        with animation:
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except sh.ErrorReturnCode as e:
                    click.get_binary_stream('stderr').write(e.stderr)
                    raise SystemExit(e.exit_code)

    built_packages = [
        tarball for tarball in glob.glob(
            os.path.join(build_artifacts, 'linux-64', '*.tar.bz2')
        ) if not os.path.basename(tarball).startswith('repodata')
    ]

    if not built_packages:
        raise click.ClickException(
            'No packages found in {}'.format(build_artifacts)
        )

    for package in tqdm.tqdm(built_packages, desc='Copying packages'):
        shutil.copyfile(
            package,
            os.path.join(artifact_directory, os.path.basename(package)),
        )
