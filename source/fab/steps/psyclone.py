# ##############################################################################
#  (c) Crown copyright Met Office. All rights reserved.
#  For further details please refer to the file COPYRIGHT
#  which you should have received as part of this distribution
# ##############################################################################
"""
A preprocessor and code generation step for PSyclone.
https://github.com/stfc/PSyclone

"""
from dataclasses import dataclass
import logging
import re
import shutil
import warnings
from itertools import chain
from pathlib import Path
from typing import Dict, List, Optional, Set

from fab.tools import run_command

from fab.artefacts import ArtefactsGetter, CollectionConcat, SuffixFilter
from fab.parse.fortran import FortranAnalyser, AnalysedFortran
from fab.parse.x90 import X90Analyser, AnalysedX90
from fab.steps import Step, check_for_errors
from fab.steps.preprocess import PreProcessor
from fab.util import log_or_dot, input_to_output_fpath, file_checksum, file_walk, TimerLogger, \
    string_checksum, suffix_filter, by_type, log_or_dot_finish

logger = logging.getLogger(__name__)


# todo: should this be part of the psyclone step?
def psyclone_preprocessor(common_flags: Optional[List[str]] = None):
    common_flags = common_flags or []

    return PreProcessor(
        # todo: use env vars and param
        preprocessor='cpp -traditional-cpp',

        source=SuffixFilter('all_source', '.X90'),
        output_collection='preprocessed_x90',

        output_suffix='.x90',
        name='preprocess x90',
        common_flags=common_flags + ['-P'],
    )


@dataclass
class MpPayload:
    """
    Runtime data for child processes to read.

    Contains data used to calculate the prebuild hash.

    """
    analysed_x90: Dict[Path, AnalysedX90]
    all_kernel_hashes: Dict[str, int]
    transformation_script_hash: int = 0


DEFAULT_SOURCE_GETTER = CollectionConcat([
    'preprocessed_x90',  # any X90 we've preprocessed this run
    SuffixFilter('all_source', '.x90'),  # any already preprocessed x90 we pulled in
])


class Psyclone(Step):
    """
    Psyclone runner step.

    This step stores prebuilt results to speed up subsequent builds.
    To generate the prebuild hashes, it analyses the X90 and kernel files, storing prebuilt results for these also.

    Kernel files are just normal Fortran, and the standard Fortran analyser is used to analyse them

    """
    def __init__(self, name=None, kernel_roots=None,
                 transformation_script: Optional[Path] = None,
                 cli_args: Optional[List[str]] = None,
                 source_getter: Optional[ArtefactsGetter] = None):
        super().__init__(name=name or 'psyclone')
        self.kernel_roots = kernel_roots or []
        self.transformation_script = transformation_script

        # "the gross switch which turns off MPI usage is a command-line argument"
        self.cli_args: List[str] = cli_args or []

        self.source_getter = source_getter or DEFAULT_SOURCE_GETTER

    @classmethod
    def tool_available(cls) -> bool:
        """Check if tje psyclone tool is available at the command line."""
        try:
            run_command(['psyclone', '-h'])
        except (RuntimeError, FileNotFoundError):
            return False
        return True

    def run(self, artefact_store: Dict, config):
        super().run(artefact_store=artefact_store, config=config)
        x90s = self.source_getter(artefact_store)

        # get the data for child processes to calculate prebuild hashes
        mp_payload = self.analysis_for_prebuilds(x90s)

        # run psyclone.
        # for every file, we get back a list of its output files plus a list of the prebuild copies.
        mp_arg = [(x90, mp_payload) for x90 in x90s]
        with TimerLogger(f"running psyclone on {len(x90s)} x90 files"):
            results = self.run_mp(mp_arg, self.do_one_file)
        log_or_dot_finish(logger)
        outputs, prebuilds = zip(*results) if results else ((), ())
        check_for_errors(outputs, caller_label=self.name)

        # flatten the list of lists we got back from run_mp
        output_files: List[Path] = list(chain(*by_type(outputs, List)))
        prebuild_files: List[Path] = list(chain(*by_type(prebuilds, List)))

        # record the output files in the artefact store for further processing
        artefact_store['psyclone_output'] = output_files
        outputs_str = "\n".join(map(str, output_files))
        logger.debug(f'psyclone outputs:\n{outputs_str}\n')

        # mark the prebuild files as being current so the cleanup step doesn't delete them
        config.add_current_prebuilds(prebuild_files)
        prebuilds_str = "\n".join(map(str, prebuild_files))
        logger.debug(f'psyclone prebuilds:\n{prebuilds_str}\n')

        # todo: delete any psy layer files which have hand-written overrides, in a given overrides folder
        # is this called psykal?
        # assert False

    # todo: test that we can run this step before or after the analysis step
    def analysis_for_prebuilds(self, x90s) -> MpPayload:
        """
        Analysis for PSyclone prebuilds.

        In order to build reusable psyclone results, we need to know everything that goes into making one.
        Then we can hash it all, and check for changes in subsequent builds.
        We'll build up this data in a payload object, to be passed to the child processes.

        Changes which must trigger reprocessing of an x90 file:
         - x90 source:
         - kernel metadata used by the x90
         - transformation script
         - cli args

        Later:
         - the psyclone version, to cover changes to built-in kernels

        Kernels:

        Kernel metadata are type definitions passed to invoke().
        For example, this x90 code depends on the kernel `compute_total_mass_kernel_type`.
        .. code-block:: fortran

            call invoke( name = "compute_dry_mass",                                         &
                         compute_total_mass_kernel_type(dry_mass, rho, chi, panel_id, qr),  &
                         sum_X(total_dry, dry_mass))

        We can see this kernel in a use statement at the top of the x90.
        .. code-block:: fortran

            use compute_total_mass_kernel_mod,   only: compute_total_mass_kernel_type

        Some kernels, such as `setval_c`, are
        `PSyclone built-ins <https://github.com/stfc/PSyclone/blob/ebb7f1aa32a9377da6ccc1ec04eec4adbc1e0a0a/src/
        psyclone/domain/lfric/lfric_builtins.py#L2136>`_.
        They will not appear in use statements and can be ignored.

        The Psyclone and Analyse steps both use the generic Fortran analyser, which recognises Psyclone kernel metadata.
        The Analysis step must come after this step because it needs to analyse the fortran we create.

        """
        # hash the transformation script
        if self.transformation_script:
            transformation_script_hash = file_checksum(self.transformation_script).file_hash
        else:
            warnings.warn('no transformation script specified')
            transformation_script_hash = 0

        # analyse the x90s
        analysed_x90 = self._analyse_x90s(x90s)

        # Analyse the kernel files, hashing the psyclone kernel metadata.
        # We only need the hashes right now but they all need analysing anyway, and we don't want to parse twice.
        # We pass them through the general fortran analyser, which currently recognises kernel metadata.
        # todo: We'd like to separate that from the general fortran analyser at some point, to reduce coupling.
        all_kernel_hashes = self._analyse_kernels(self.kernel_roots)

        return MpPayload(
            transformation_script_hash=transformation_script_hash,
            analysed_x90=analysed_x90,
            all_kernel_hashes=all_kernel_hashes
        )

    def _analyse_x90s(self, x90s: Set[Path]) -> Dict[Path, AnalysedX90]:
        # Analyse parsable versions of the x90s, finding kernel dependencies.

        # make parsable - todo: fast enough not to require prebuilds?
        with TimerLogger(f"converting {len(x90s)} x90s into parsable fortran"):
            parsable_x90s = self.run_mp(items=x90s, func=make_parsable_x90)

        # parse
        x90_analyser = X90Analyser()
        x90_analyser._config = self._config
        with TimerLogger(f"analysing {len(parsable_x90s)} parsable x90 files"):
            x90_results = self.run_mp(items=parsable_x90s, func=x90_analyser.run)
        log_or_dot_finish(logger)
        x90_analyses, x90_artefacts = zip(*x90_results) if x90_results else ((), ())
        check_for_errors(results=x90_analyses)

        # mark the analysis results files (i.e. prebuilds) as being current, so the cleanup knows not to delete them
        prebuild_files = list(by_type(x90_artefacts, Path))
        self._config.add_current_prebuilds(prebuild_files)

        # record the analysis results against the original x90 filenames (not the parsable versions we analysed)
        analysed_x90 = by_type(x90_analyses, AnalysedX90)
        analysed_x90 = {result.fpath.with_suffix('.x90'): result for result in analysed_x90}

        # make the hashes from the original x90s, not the parsable versions which have invoke names removed.
        for p, r in analysed_x90.items():
            analysed_x90[p]._file_hash = file_checksum(p).file_hash

        return analysed_x90

    def _analyse_kernels(self, kernel_roots) -> Dict[str, int]:
        # We want to hash the kernel metadata (type defs).
        # Ignore the prebuild folder. Todo: test the prebuild folder is ignored, in case someone breaks this.
        file_lists = [file_walk(root, ignore_folders=[self._config.prebuild_folder]) for root in kernel_roots]
        all_kernel_files: Set[Path] = set(*chain(file_lists))
        kernel_files: List[Path] = suffix_filter(all_kernel_files, ['.f90'])

        # We use the normal Fortran analyser, which records psyclone kernel metadata.
        # todo: We'd like to separate that from the general fortran analyser at some point, to reduce coupling.
        # The Analyse step also uses the same fortran analyser. It stores its results so they won't be analysed twice.
        fortran_analyser = FortranAnalyser()
        fortran_analyser._config = self._config
        with TimerLogger(f"analysing {len(kernel_files)} potential psyclone kernel files"):
            fortran_results = self.run_mp(items=kernel_files, func=fortran_analyser.run)
        log_or_dot_finish(logger)
        fortran_analyses, fortran_artefacts = zip(*fortran_results) if fortran_results else (tuple(), tuple())

        errors: List[Exception] = list(by_type(fortran_analyses, Exception))
        if errors:
            errs_str = '\n\n'.join(map(str, errors))
            logger.error(f"There were {len(errors)} errors while parsing kernels:\n\n{errs_str}")

        # mark the analysis results files (i.e. prebuilds) as being current, so the cleanup knows not to delete them
        prebuild_files = list(by_type(fortran_artefacts, Path))
        self._config.add_current_prebuilds(prebuild_files)

        analysed_fortran: List[AnalysedFortran] = list(by_type(fortran_analyses, AnalysedFortran))

        # gather all kernel hashes into one big lump
        all_kernel_hashes: Dict[str, int] = {}
        for af in analysed_fortran:
            assert set(af.psyclone_kernels).isdisjoint(all_kernel_hashes), \
                f"duplicate kernel name(s): {set(af.psyclone_kernels) & set(all_kernel_hashes)}"
            all_kernel_hashes.update(af.psyclone_kernels)

        return all_kernel_hashes

    def do_one_file(self, arg):
        x90_file, mp_payload = arg
        prebuild_hash = self._gen_prebuild_hash(x90_file, mp_payload)

        # These are the filenames we expect to be output for this x90 input file.
        # There will always be one modified_alg, and 0+ generated.
        modified_alg = x90_file.with_suffix('.f90')
        modified_alg = input_to_output_fpath(config=self._config, input_path=modified_alg)
        generated = x90_file.parent / (str(x90_file.stem) + '_psy.f90')
        generated = input_to_output_fpath(config=self._config, input_path=generated)

        generated.parent.mkdir(parents=True, exist_ok=True)

        # todo: do we have handwritten overrides?

        # do we already have prebuilt results for this x90 file?
        prebuilt_alg, prebuilt_gen = self._get_prebuild_paths(modified_alg, generated, prebuild_hash)
        if prebuilt_alg.exists():
            # todo: error handling in here
            msg = f'found prebuilds for {x90_file}:\n    {prebuilt_alg}'
            shutil.copy2(prebuilt_alg, modified_alg)
            if prebuilt_gen.exists():
                msg += f'\n    {prebuilt_gen}'
                shutil.copy2(prebuilt_gen, generated)
            log_or_dot(logger=logger, msg=msg)

        else:
            try:
                # logger.info(f'running psyclone on {x90_file}')
                self.run_psyclone(generated, modified_alg, x90_file)

                shutil.copy2(modified_alg, prebuilt_alg)
                msg = f'created prebuilds for {x90_file}:\n    {prebuilt_alg}'
                if Path(generated).exists():
                    msg += f'\n    {prebuilt_gen}'
                    shutil.copy2(generated, prebuilt_gen)
                log_or_dot(logger=logger, msg=msg)

            except Exception as err:
                logger.error(err)
                return err, None

        # return the output files from psyclone
        result: List[Path] = [modified_alg]
        if Path(generated).exists():
            result.append(generated)

        # we also want to return the prebuild artefact files we created,
        # which are just copies, in the prebuild folder, with hashes in the filenames.
        prebuild_result: List[Path] = [prebuilt_alg, prebuilt_gen]

        return result, prebuild_result

    def _gen_prebuild_hash(self, x90_file: Path, mp_payload: MpPayload):
        """
        Calculate the prebuild hash for this x90 file, based on all the things which should trigger reprocessing.

        """
        # We've analysed (a parsable version of) this x90.
        analysis_result = mp_payload.analysed_x90[x90_file]  # type: ignore

        # include the hashes of kernels used by this x90
        kernel_deps_hashes = {
            mp_payload.all_kernel_hashes[kernel_name] for kernel_name in analysis_result.kernel_deps}  # type: ignore

        # hash everything which should trigger re-processing
        # todo: hash the psyclone version in case the built-in kernels change?
        prebuild_hash = sum([

            # the hash of the x90 (not of the parsable version, so includes invoke names)
            analysis_result.file_hash,

            # the hashes of the kernels used by this x90
            sum(kernel_deps_hashes),

            #
            mp_payload.transformation_script_hash,

            # command-line arguments
            string_checksum(str(self.cli_args)),
        ])

        return prebuild_hash

    def _get_prebuild_paths(self, modified_alg, generated, prebuild_hash):
        prebuilt_alg = Path(self._config.prebuild_folder / f'{modified_alg.stem}.{prebuild_hash}{modified_alg.suffix}')
        prebuilt_gen = Path(self._config.prebuild_folder / f'{generated.stem}.{prebuild_hash}{generated.suffix}')
        return prebuilt_alg, prebuilt_gen

    def run_psyclone(self, generated, modified_alg, x90_file):

        # -d specifies "a root directory structure containing kernel source"
        kernel_args = sum([['-d', k] for k in self.kernel_roots], [])

        # transformation python script
        transform_options = ['-s', self.transformation_script] if self.transformation_script else []

        command = [
            'psyclone', '-api', 'dynamo0.3',
            '-l', 'all',
            *kernel_args,
            '-opsy', generated,  # filename of generated PSy code
            '-oalg', modified_alg,  # filename of transformed algorithm code
            *transform_options,
            *self.cli_args,
            x90_file,
        ]

        run_command(command)


# regex to convert an x90 into parsable fortran, so it can be analysed using a third party tool

WHITE = r'[\s&]+'
OPT_WHITE = r'[\s&]*'

SQ_STRING = "'[^']*'"
DQ_STRING = '"[^"]*"'
STRING = f'({SQ_STRING}|{DQ_STRING})'

NAME_KEYWORD = 'name' + OPT_WHITE + '=' + OPT_WHITE + STRING + OPT_WHITE + ',' + OPT_WHITE
NAMED_INVOKE = 'call' + WHITE + 'invoke' + OPT_WHITE + r'\(' + OPT_WHITE + NAME_KEYWORD

_x90_compliance_pattern = None


# todo: In the future, we'd like to extend fparser to handle the leading invoke keywords. (Lots of effort.)
def make_parsable_x90(x90_path: Path) -> Path:
    """
    Take out the leading name keyword in calls to invoke(), making temporary, parsable fortran from x90s.

    If present it looks like this::

        call invoke( name = "compute_dry_mass", ...

    Returns the path of the parsable file.

    This function is not slow so we're not creating prebuilds for this work.

    """
    global _x90_compliance_pattern
    if not _x90_compliance_pattern:
        _x90_compliance_pattern = re.compile(pattern=NAMED_INVOKE)

    # src = open(x90_path, 'rt').read()

    # Before we remove the name keywords to invoke, we must remove any comment lines.
    # This is the simplest way to avoid producing bad fortran when the name keyword is followed by a comment line.
    # I.e. The comment line doesn't have an "&", so we get "call invoke(!" with no "&", which is a syntax error.
    src_lines = open(x90_path, 'rt').readlines()
    no_comment_lines = [line for line in src_lines if not line.lstrip().startswith('!')]
    src = ''.join(no_comment_lines)

    replaced = []

    def repl(matchobj):
        # matchobj[0] contains the entire matching string, from "call" to the "," after the name keyword.
        # matchobj[1] contains the single group in the search pattern, which is defined in STRING.
        name = matchobj[1].replace('"', '').replace("'", "")
        replaced.append(name)
        return 'call invoke('

    out = _x90_compliance_pattern.sub(repl=repl, string=src)

    out_path = x90_path.with_suffix('.parsable_x90')
    open(out_path, 'wt').write(out)

    logger.debug(f'names removed from {str(x90_path)}: {replaced}')

    return out_path
