#!/usr/bin/env python3
##############################################################################
# (c) Crown copyright Met Office. All rights reserved.
# For further details please refer to the file COPYRIGHT
# which you should have received as part of this distribution
##############################################################################
from datetime import timedelta

from fab.build_config import BuildConfig
from fab.steps.cleanup_prebuilds import CleanupPrebuilds
from fab.steps.compile_fortran import get_fortran_compiler
from fab.steps.link import LinkSharedObject

from gcom_build_steps import common_build_steps, parse_args


def gcom_so_config(revision=None, compiler=None):
    """
    Create a shared object for linking.

    """
    # We want a separate project folder for each compiler. Find out which compiler we'll be using.
    compiler, _ = get_fortran_compiler(compiler)

    config = BuildConfig(
        project_label=f'gcom shared library {revision} {compiler}',
        steps=[
            *common_build_steps(revision=revision, fortran_compiler=compiler, fpic=True),
            LinkSharedObject(output_fpath='$output/libgcom.so'),

            CleanupPrebuilds(older_than=timedelta(minutes=5))
        ],
        # verbose=True,
    )

    return config


if __name__ == '__main__':
    args = parse_args()
    gcom_so_config(compiler=args.compiler, revision=args.revision).run()
