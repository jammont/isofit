"""
These tests are to ensure any changes to the CLI will be backwards compatible.
"""
import io
import json
import os
import pathlib
import shutil
import zipfile

import pytest
import requests
from click.testing import CliRunner

from isofit import cli
from isofit.utils import surface_model

# Environment variables
EMULATOR_PATH = os.environ.get("EMULATOR_PATH", "")
CORES = os.cpu_count()


@pytest.fixture(scope="session")
def cube_example(tmp_path_factory):
    """
    Downloads the medium cube example's data
    """
    url = "https://avng.jpl.nasa.gov/pub/PBrodrick/isofit/test_data_rev.zip"
    path = tmp_path_factory.mktemp("cube_example")

    r = requests.get(url)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    z.extractall(path)

    return path


@pytest.fixture(scope="session")
def surface(cube_example):
    """
    Generates the surface.mat file
    """
    path = pathlib.Path(__file__).parent
    with open(path / "data/surface.json") as f:
        conf = json.load(f)

    # Update the json file with proper paths relative to the given system
    # fmt: off
    conf["output_model_file"] = str(cube_example / "surface.mat")
    conf["wavelength_file"] = str(
        (path/"../../examples/20171108_Pasadena/remote/20170320_ang20170228_wavelength_fit.txt").resolve()
    )
    conf["wavelength_file"] = str(
        (path/ "../../examples/20171108_Pasadena/remote/20170320_ang20170228_wavelength_fit.txt").resolve()
    )
    conf["sources"][0]["input_spectrum_files"][0] = str(
        (path / "../../data/reflectance/surface_model_ucsb").resolve()
    )
    # fmt: on

    with open(cube_example / "surface.json", "w") as f:
        json.dump(conf, f)

    # Generate the surface.mat
    surface_model(cube_example / "surface.json")

    # Return the path to the mat file
    return str(cube_example / "surface.mat")


@pytest.fixture()
def files(cube_example):
    """
    Common data files to be used by multiple tests. The return is a list in the
    order: [
        0: Radiance file,
        1: Location file,
        2: Observation file,
        3: Output directory
    ]

    As of 07/24/2023 these are from the medium cube example.
    """
    # Flush the output dir if it already exists from a previous test case
    output = cube_example / "output"
    shutil.rmtree(output, ignore_errors=True)

    return [
        str(cube_example / "medium_chunk/ang20170323t202244_rdn_7k-8k"),
        str(cube_example / "medium_chunk/ang20170323t202244_loc_7k-8k"),
        str(cube_example / "medium_chunk/ang20170323t202244_obs_7k-8k"),
        str(output),
    ]


# fmt: off
@pytest.mark.parametrize("args", [
    ["ang", "--presolve", 1, "--emulator_base", EMULATOR_PATH, "--n_cores", CORES, "--analytical_line", 1, "-nn", 10, "-nn", 50,],
    ["ang", "--presolve", 1, "--emulator_base", EMULATOR_PATH, "--n_cores", CORES, "--analytical_line", 1, "-nn", 10, "-nn", 50, "-nn", 10, "--pressure_elevation",],
    ["ang", "--presolve", 1, "--emulator_base", EMULATOR_PATH, "--n_cores", CORES, "--empirical_line", 1, "--surface_category", "glint_surface",],
])
# fmt: on
def test_apply_oe(files, args, surface):
    """
    Executes the isofit apply_oe cli command for various test cases
    """
    runner = CliRunner()
    result = runner.invoke(
        cli, ["apply_oe"] + files + args + ["--surface_path", surface]
    )

    if result.exception:
        print(f"Test case hit an exception: {result.exception}")
        print(f"Output for this test case:\n{result.output}")

    assert result.exit_code == 0