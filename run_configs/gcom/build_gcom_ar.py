#!/usr/bin/env python3
##############################################################################
# (c) Crown copyright Met Office. All rights reserved.
# For further details please refer to the file COPYRIGHT
# which you should have received as part of this distribution
##############################################################################
from fab.build_config import BuildConfig
from fab.steps.archive_objects import ArchiveObjects
from fab.steps.cleanup_prebuilds import CleanupPrebuilds
from fab.steps.compile_fortran import get_fortran_compiler

from gcom_build_steps import common_build_steps, parse_args


def gcom_ar_config(revision=None, compiler=None):
    """
    Create an object archive for linking.

    """
    # We want a separate project folder for each compiler. Find out which compiler we'll be using.
    compiler, _ = get_fortran_compiler(compiler)

    config = BuildConfig(
        project_label=f'gcom object archive {revision} {compiler}',
        steps=[
            *common_build_steps(revision=revision, fortran_compiler=compiler),
            ArchiveObjects(output_fpath='$output/libgcom.a'),

            CleanupPrebuilds(all_unused=True),
        ],
    )

    return config


if __name__ == '__main__':
    args = parse_args()
    gcom_ar_config(revision=args.revision, compiler=args.compiler).run()
