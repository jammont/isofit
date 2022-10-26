#! /usr/bin/env python3
#
#  Copyright 2019 California Institute of Technology
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# ISOFIT: Imaging Spectrometer Optimal FITting
# Author: Philip G. Brodrick, philip.brodrick@jpl.nasa.gov
#

from scipy.linalg import inv
from isofit.core.fileio import write_bil_chunk
from isofit.core.instrument import Instrument
from spectral.io import envi
from scipy.spatial import KDTree
import numpy as np
import logging
import time
import matplotlib
import pylab as plt
from isofit.configs import configs
import ray
import atexit
from isofit.core.common import envi_header
from scipy.ndimage import gaussian_filter

plt.switch_backend("Agg")


@ray.remote
def _run_chunk(start_line: int, stop_line: int, reference_state_file: str, reference_locations_file: str,
               input_locations_file: str, segmentation_file: str, output_atm_file: str,
               nneighbors: int, nodata_value: float, loglevel: str, logfile: str) -> None:
    """
    Args:
        start_line: line to start empirical line run at
        stop_line:  line to stop empirical line run at
        reference_radiance_file: source file for radiance (interpolation built from this)
        reference_reflectance_file:  source file for reflectance (interpolation built from this)
        reference_uncertainty_file:  source file for uncertainty (interpolation built from this)
        reference_locations_file:  source file for file locations (lon, lat, elev), (interpolation built from this)
        input_radiance_file: input radiance file (interpolate over this)
        input_locations_file: input location file (interpolate over this)
        segmentation_file: input file noting the per-pixel segmentation used
        isofit_config: path to isofit configuration JSON file
        output_reflectance_file: location to write output reflectance to
        output_uncertainty_file: location to write output uncertainty to
        radiance_factors: radiance adjustment factors
        nneighbors: number of neighbors to use for interpolation
        nodata_value: nodata value of input and output
        loglevel: logging level
        logfile: logging file

    Returns:
        None

    """

    logging.basicConfig(format='%(levelname)s:%(asctime)s ||| %(message)s', level=loglevel, filename=logfile, datefmt='%Y-%m-%d,%H:%M:%S')

    # Load reference images
    reference_state_img = envi.open(envi_header(reference_state_file), reference_state_file)
    reference_locations_img = envi.open(envi_header(reference_locations_file), reference_locations_file)

    n_reference_lines, n_state_bands, n_reference_columns = [int(reference_state_img.metadata[n])
                                                                for n in ('lines', 'bands', 'samples')]

    # Load input images
    input_locations_img = envi.open(envi_header(input_locations_file), input_locations_file)
    n_location_bands = int(input_locations_img.metadata['bands'])
    n_input_samples = input_locations_img.shape[1]
    n_input_lines = input_locations_img.shape[0]

    # Load output images

    # Load reference data
    reference_locations_mm = reference_locations_img.open_memmap(interleave='bip', writable=False)
    reference_locations = np.array(reference_locations_mm[:, :, :]).reshape((n_reference_lines, n_location_bands))

    atm_bands = np.where(np.array([x[:4] != 'RFL_' for x in reference_state_img.metadata['band names']]))[0]
    n_atm_bands = len(atm_bands)
    reference_state_mm = reference_state_img.open_memmap(interleave='bip', writable=False)
    reference_state = np.array(reference_state_mm[:, :, atm_bands]).reshape((n_reference_lines, n_atm_bands))

    # Load segmentation data
    if segmentation_file:
        segmentation_img = envi.open(envi_header(segmentation_file), segmentation_file)
        segmentation_img = segmentation_img.read_band(0)
    else:
        segmentation_img = None

    # Load Tree
    loc_scaling = np.array([1e6, 1e6, 0.01])
    scaled_ref_loc = reference_locations * loc_scaling
    tree = KDTree(scaled_ref_loc)
    # Assume (heuristically) that, for distance purposes, 1 m vertically is
    # comparable to 10 m horizontally, and that there are 100 km per latitude
    # degree.  This is all approximate of course.  Elevation appears in the
    # Third element, and the first two are latitude/longitude coordinates

    # Iterate through image
    hash_table = {}

    for row in np.arange(start_line, stop_line):

        # Load inline input data
        input_locations_mm = input_locations_img.open_memmap(
            interleave='bip', writable=False)
        input_locations = np.array(input_locations_mm[row, :, :])

        output_atm_row = np.zeros((n_input_samples, len(atm_bands))) + nodata_value

        nspectra, start = 0, time.time()
        for col in np.arange(n_input_samples):

            x = input_locations[col, :]
            if np.all(np.isclose(x, nodata_value)):
                output_atm_row[col, :] = nodata_value
                continue
            else:
                x *= loc_scaling

            bhat = None
            hash_idx = segmentation_img[row, col]
            if hash_idx in hash_table:
                bhat = hash_table[hash_idx]

            if bhat is None:
                dists, nn = tree.query(x, nneighbors)
                xv = reference_locations[nn, :]*loc_scaling[np.newaxis,:]
                yv = reference_state[nn, :]

                bhat = np.zeros((n_atm_bands, xv.shape[1]))

                for i in np.arange(n_atm_bands):
                    use = yv[:, i] > -5
                    n = sum(use)
                    # only use lat/lon here, ignore Z
                    X = np.concatenate((np.ones((n, 1)), xv[use, :-1]), axis=1)
                    W = np.diag(np.ones(n))  # /uv[use, i])
                    y = yv[use, i:i + 1]
                    try:
                        bhat[i, :] = (inv(X.T @ W @ X) @ X.T @ W @ y).T
                    except:
                        bhat[i, :] = 0

                    #if i == 0:
                    #    print(X, y, bhat)

            if (segmentation_img is not None) and not (hash_idx in hash_table):
                hash_table[hash_idx] = bhat

            A = np.hstack((np.ones(1), x[:-1]))
            output_atm_row[col,:] = (bhat.T * A[:,np.newaxis]).sum(axis=0)

            nspectra = nspectra + 1

        elapsed = float(time.time() - start)
        logging.debug('row {}/{}, ({}/{} local), {} spectra per second'.format(row, n_input_lines, int(row - start_line),
                                                                              int(stop_line - start_line),
                                                                              round(float(nspectra) / elapsed, 2)))

        del input_locations_mm

        output_atm_row = output_atm_row.transpose((1, 0))

        write_bil_chunk(output_atm_row, output_atm_file, row,
                         (n_input_lines, n_atm_bands, n_input_samples))



def atm_interpolation(reference_state_file: str, 
                      reference_locations_file: str, segmentation_file: str, 
                      input_locations_file: str, output_atm_file: str,
                      nneighbors: int = 400, nodata_value: float = -9999.0, level: str = 'INFO', logfile: str = None,
                      n_cores: int = -1, gaussian_smoothing_sigma=2) -> None:
    """
    Perform an empirical line interpolation for reflectance and uncertainty extrapolation
    Args:
        reference_radiance_file: source file for radiance (interpolation built from this)
        reference_reflectance_file:  source file for reflectance (interpolation built from this)
        reference_uncertainty_file:  source file for uncertainty (interpolation built from this)
        reference_locations_file:  source file for file locations (lon, lat, elev), (interpolation built from this)
        segmentation_file: input file noting the per-pixel segmentation used
        input_radiance_file: input radiance file (interpolate over this)
        input_locations_file: input location file (interpolate over this)
        output_reflectance_file: location to write output reflectance to
        output_uncertainty_file: location to write output uncertainty to

        nneighbors: number of neighbors to use for interpolation
        nodata_value: nodata value of input and output
        level: logging level
        logfile: logging file
        radiance_factors: radiance adjustment factors
        isofit_config: path to isofit configuration JSON file
        n_cores: number of cores to run on
        reference_class_file: optional source file for sub-type-classifications, in order: [base, cloud, water]
    Returns:
        None
    """

    loglevel = level

    logging.basicConfig(format='%(levelname)s:%(asctime)s ||| %(message)s', level=loglevel, filename=logfile, datefmt='%Y-%m-%d,%H:%M:%S')

    reference_state_img = envi.open(envi_header(reference_state_file))
    input_locations_img = envi.open(envi_header(input_locations_file))
    n_input_lines = int(input_locations_img.metadata['lines'])
    n_input_samples = int(input_locations_img.metadata['samples'])

    # Create output files
    output_metadata = reference_state_img.metadata
    output_metadata['interleave'] = 'bil'
    output_metadata['lines'] = input_locations_img.metadata['lines']
    output_metadata['samples'] = input_locations_img.metadata['samples']

    band_names = [x for x in reference_state_img.metadata['band names'] if x[:4] != 'RFL_' ]
    output_metadata['band names'] = band_names
    output_metadata['description'] = 'Interpolated atmospheric state'
    output_metadata['bands'] = len(band_names)

    output_atm_img = envi.create_image(envi_header(output_atm_file), ext='',
                                       metadata=output_metadata, force=True)

    # Now cleanup inputs and outputs, we'll write dynamically above
    del output_atm_img
    del reference_state_img, input_locations_img

    # Initialize ray cluster
    start_time = time.time()
    rayargs = {'ignore_reinit_error': True,
               'local_mode': n_cores == 1}
    if n_cores != -1:
        ray_argw['num_cpus'] = n_cores

    ray.init(**rayargs)
    atexit.register(ray.shutdown)

    n_ray_cores = int(ray.available_resources()["CPU"])
    n_cores = min(n_ray_cores, n_input_lines)

    logging.info('Beginning atmospheric interpolation {} cores'.format(n_cores))

    # Break data into sections
    line_sections = np.linspace(0, n_input_lines, num=int(n_cores + 1), dtype=int)

    start_time = time.time()

    # Run the pool (or run serially)
    results = []
    for l in range(len(line_sections) - 1):
        args = (line_sections[l], line_sections[l + 1], reference_state_file, 
                reference_locations_file, 
                input_locations_file, segmentation_file, output_atm_file,
                nneighbors, nodata_value, level, logfile)
        results.append(_run_chunk.remote(*args))

    _ = ray.get(results)

    total_time = time.time() - start_time
    logging.info('Parallel atmospheric interpolations complete.  {} s total, {} spectra/s, {} spectra/s/core'.format(
        total_time, line_sections[-1] * n_input_samples / total_time,
                    line_sections[-1] * n_input_samples / total_time / n_cores))


    atm_img = envi.open(envi_header(output_atm_file)).open_memmap(interleave='bip').copy()

    if gaussian_smoothing_sigma > 0:
        for n in range(atm_img.shape[-1]):
            null = atm_img[...,n] == -9999
            V=atm_img[...,n]
            V[null]=0
            VV=gaussian_filter(V,sigma=gaussian_smoothing_sigma)

            W=0*atm_img[...,n]+1
            W[null]=0
            WW=gaussian_filter(W,sigma=gaussian_smoothing_sigma)

            smoothed=VV/WW
            atm_img[...,n] = smoothed

        atm_img = atm_img.transpose((0,2,1))
        write_bil_chunk(atm_img, output_atm_file, 0, atm_img.shape)






