from datetime import datetime
from enum import IntEnum
import logging
import netCDF4
import numpy as np
import pandas as pd
import sqlite3
import xarray as xr
from xarray.coding.times import encode_cf_datetime


LOG = logging.getLogger(__package__)


TIME_COVERAGE = {
    'METOP-A': (datetime(2007, 6, 28, 23, 14), None),
    'METOP-B': (datetime(2013, 1, 1, 1, 1), None),
    'NOAA-6': (datetime(1980, 1, 1, 0, 0), datetime(1982, 8, 3, 0, 39)),
    'NOAA-7': (datetime(1981, 8, 24, 0, 13), datetime(1985, 2, 1, 22, 21)),
    'NOAA-8': (datetime(1983, 5, 4, 19, 9), datetime(1985, 10, 14, 3, 26)),
    'NOAA-9': (datetime(1985, 2, 25, 0, 13), datetime(1988, 11, 7, 21, 18)),
    'NOAA-10': (datetime(1986, 11, 17, 1, 22), datetime(1991, 9, 16, 21, 19)),
    'NOAA-11': (datetime(1988, 11, 8, 0, 16), datetime(1994, 10, 16, 23, 27)),
    'NOAA-12': (datetime(1991, 9, 16, 0, 17), datetime(1998, 12, 14, 20, 43)),
    'NOAA-14': (datetime(1995, 1, 20, 0, 37), datetime(2002, 10, 7, 22, 47)),
    'NOAA-15': (datetime(1998, 10, 26, 0, 54), None),
    'NOAA-16': (datetime(2001, 1, 1, 0, 0), datetime(2011, 12, 31, 23, 40)),
    'NOAA-17': (datetime(2002, 6, 25, 5, 41), datetime(2011, 12, 31, 19, 11)),
    'NOAA-18': (datetime(2005, 5, 20, 18, 17), None),
    'NOAA-19': (datetime(2009, 2, 6, 18, 32), None),
    'TIROS-N': (datetime(1978, 11, 5, 9, 8), datetime(1980, 1, 30, 17, 3))
}  # Estimated based on NOAA L1B archive


class QualityFlags(IntEnum):
    OK = 0
    INVALID_TIMESTAMP = 1  # end time < start time or timestamp out of valid range
    TOO_SHORT = 2  # not enough scanlines or duration too short
    TOO_LONG = 3  # (end_time - start_time) unrealistically large
    DUPLICATE = 4  # identical record from different ground stations
    REDUNDANT = 5  # subset of another file


FILL_VALUE_INT = -9999
FILL_VALUE_FLOAT = -9999.

ADDITIONAL_METADATA = [
    {'name': 'overlap_free_start',
     'long_name': 'First scanline (0-based) of the overlap-free part of this file. Scanlines '
                  'before that also appear in the preceding file.',
     'dtype': np.int16,
     'fill_value': FILL_VALUE_INT},
    {'name': 'overlap_free_end',
     'long_name': 'Last scanline (0-based) of the overlap-free part of this file. Scanlines '
                  'hereafter also appear in the subsequent file.',
     'dtype': np.int16,
     'fill_value': FILL_VALUE_INT},
    {'name': 'midnight_line',
     'long_name': 'Scanline (0-based) where UTC timestamp crosses the dateline',
     'dtype': np.int16,
     'fill_value': FILL_VALUE_INT},
    {'name': 'equator_crossing_longitude',
     'long_name': 'Longitude where ascending node crosses the equator',
     'units': 'degrees_east',
     'dtype': np.float64,
     'fill_value': FILL_VALUE_FLOAT},
    {'name': 'equator_crossing_time',
     'long_name': 'UTC time when ascending node crosses the equator',
     'units': 'seconds since 1970-01-01 00:00:00',
     'calendar': 'standard',
     'dtype': np.float64,
     'fill_value': FILL_VALUE_INT},
    {'name': 'global_quality_flag',
     'long_name': 'Global quality flag',
     'comment': 'If this flag is everything else than "ok", it is recommended not '
                'to use the file.',
     'flag_values': [flag.value for flag in QualityFlags.__members__.values()],
     'flag_meanings': [name.lower() for name in QualityFlags.__members__.keys()],
     'dtype': np.uint8,
     'fill_value': None}
]


class MetadataCollector:
    """Collect and complement metadata from level 1c files.

    Additional metadata include global quality flags as well equator crossing time and
    overlap information.
    """
    def __init__(self, min_num_lines=50, min_duration=5):
        """
        Args:
            min_num_lines: Minimum number of scanlines for a file to be considered ok. Otherwise
                           it will flagged as too short.
            min_duration: Minimum duration (in minutes) for a file to be considered ok. Otherwise
                          it will flagged as too short.
        """
        self.min_num_lines = min_num_lines
        self.min_duration = np.timedelta64(min_duration, 'm')

    def get_metadata(self, filenames):
        """Collect and complement metadata from the given level 1c files."""
        LOG.info('Collecting metadata')
        df = pd.DataFrame(self._collect_metadata(filenames))
        df.sort_values(by=['start_time', 'end_time'], inplace=True)

        # Set quality flags
        LOG.info('Computing quality flags')
        df = df.groupby('platform').apply(lambda x: self._set_global_qual_flags(x, x.name))
        df = df.drop(['platform'], axis=1)

        # Calculate overlap
        LOG.info('Computing overlap')
        df = df.groupby('platform').apply(lambda x: self._calc_overlap(x))

        return df

    def save_sql(self, mda, dbfile):
        """Save metadata to sqlite database."""
        con = sqlite3.connect(dbfile)
        mda.to_sql(name='metadata', con=con, if_exists='replace')
        con.commit()
        con.close()

    def read_sql(self, dbfile):
        """Read metadata from sqlite database."""
        with sqlite3.connect(dbfile) as con:
            mda = pd.read_sql('select * from metadata', con)
        mda = mda.set_index(['platform', 'level_1'])
        for time_col in ['start_time', 'end_time', 'equator_crossing_time']:
            mda[time_col] = mda[time_col].astype('datetime64[ns]')
        return mda

    def _collect_metadata(self, filenames):
        """Collect metadata from the given level 1c files."""
        records = []
        for filename in filenames:
            LOG.debug('Collecting metadata from {}'.format(filename))
            with xr.open_dataset(filename) as ds:
                midnight_line = np.float64(self._get_midnight_line(ds['acq_time']))
                eq_cross_lon, eq_cross_time = self._get_equator_crossing(ds)
                rec = {'platform':  ds.attrs['platform'].split('>')[-1].strip(),
                       'start_time': ds['acq_time'].values[0],
                       'end_time': ds['acq_time'].values[-1],
                       'along_track': ds.dims['y'],
                       'filename': filename,
                       'equator_crossing_longitude': eq_cross_lon,
                       'equator_crossing_time': eq_cross_time,
                       'midnight_line': midnight_line,
                       'overlap_free_start': np.nan,
                       'overlap_free_end': np.nan,
                       'global_quality_flag': QualityFlags.OK}
                records.append(rec)
        return records

    def _get_midnight_line(self, acq_time):
        """Find scanline where the UTC date increases by one day.

        Returns:
            int: The midnight scanline if it exists.
                 None, else.
        """
        d0 = np.datetime64('1970-01-01', 'D')
        days = (acq_time.astype('datetime64[D]') - d0) / np.timedelta64(1, 'D')
        incr = np.where(np.diff(days) == 1)[0]
        if len(incr) >= 1:
            if len(incr) > 1:
                LOG.warning('UTC date increases more than once. Choosing the first '
                            'occurence as midnight scanline.')
            return incr[0]
        return np.nan

    def _get_equator_crossing(self, ds):
        """Determine where the ascending node crosses the equator.

        Returns:
            Longitude and UTC time
        """
        # Use coordinates in the middle of the swath
        mid_swath = ds['latitude'].shape[1] // 2
        lat = ds['latitude'].isel(x=mid_swath)
        lat_shift = lat.shift(y=-1, fill_value=lat.isel(y=-1))
        sign_change = np.sign(lat_shift) != np.sign(lat)
        ascending = lat_shift > lat
        lat_eq = lat.where(sign_change & ascending, drop=True)
        if len(lat_eq) > 0:
            return lat_eq['longitude'].values[0], lat_eq['acq_time'].values[0]
        return np.nan, np.datetime64('NaT')

    def _set_redundant_flag(self, df, window=20):
        """Flag redundant files in the given data frame.

        An file is called redundant if it is entirely overlapped by one of its predecessors
        (in time).

        Args:
            window (int): Number of preceding files to be taken into account

        TODO: Identify the following case as redundant, as it causes overlap_free_start to be
        greater than overlap_free_end:

        |-------|  previous file
            |--------|  current file
              |---------|  subsequent file
        """
        def is_redundant(end_times):
            start_times = end_times.index.get_level_values('start_time').to_numpy()
            end_times = end_times.to_numpy()
            redundant = (start_times[-1] >= start_times) & (end_times[-1] <= end_times)
            redundant[-1] = False
            return redundant.any()

        # Only take into account files that passed the QC check so far (e.g. we don't want
        # files flagged as TOO_LONG to overlap many subsequent files)
        df_ok = df[df['global_quality_flag'] == QualityFlags.OK].copy()

        # DataFrame.rolling is an elegant solution, but it has two drawbacks:
        # a) It only supports numerical data types. Workaround: Convert timestamps to integer.
        df_ok['start_time'] = df_ok['start_time'].astype(np.int64)
        df_ok['end_time'] = df_ok['end_time'].astype(np.int64)

        # b) DataFrame.rolling().apply() only has access to one column at a time. Workaround: Move
        #    start_time to the index and pass the end_time series - including the index - to our
        #    function. This can be achieved by calling apply(..., raw=False).
        df_ok = df_ok.set_index('start_time', append=True)
        rolling = df_ok['end_time'].rolling(window, min_periods=2)
        redundant = rolling.apply(is_redundant, raw=False).fillna(0).astype(np.bool)
        redundant = redundant.reset_index('start_time', drop=True)

        # So far we have operated on the qc-passed rows only. Update quality flags of rows in the
        # original (full) data frame.
        redundant = redundant[redundant.astype(np.bool)]
        df.loc[redundant.index, 'global_quality_flag'] = QualityFlags.REDUNDANT

    def _set_duplicate_flag(self, df):
        """Flag duplicate files in the given data frame.

        Two files are considered equal if platform, start- and end-time are identical. This happens
        if the same measurement has been transferred to two different ground stations.
        """
        gs_dupl = df.duplicated(subset=['platform', 'start_time', 'end_time'],
                                keep='first')
        df.loc[gs_dupl, 'global_quality_flag'] = QualityFlags.DUPLICATE

    def _set_invalid_timestamp_flag(self, df, platform):
        """Flag files with invalid timestamps.

        Timestamps are considered invalid if they are outside the temporal coverage of the platform
        or if end_time < start_time.
        """
        valid_min, valid_max = TIME_COVERAGE[platform]
        if not valid_max:
            valid_max = np.datetime64('2030-01-01 00:00')
        valid_min = np.datetime64(valid_min)
        valid_max = np.datetime64(valid_max)
        out_of_range = ((df['start_time'] < valid_min) |
                        (df['start_time'] > valid_max) |
                        (df['end_time'] < valid_min) |
                        (df['end_time'] > valid_max))
        neg_dur = df['end_time'] < df['start_time']
        invalid = neg_dur | out_of_range
        df.loc[invalid, 'global_quality_flag'] = QualityFlags.INVALID_TIMESTAMP

    def _set_too_short_flag(self, df):
        """Flag files considered too short.

        That means either not enough scanlines or duration is too short.
        """
        too_short = (
                (df['along_track'] < self.min_num_lines) |
                (abs(df['end_time'] - df['start_time']) < self.min_duration)
        )
        df.loc[too_short, 'global_quality_flag'] = QualityFlags.TOO_SHORT

    def _set_too_long_flag(self, df, max_length=120):
        """Flag files where (end_time - start_time) is unrealistically large.

        This happens if the timestamps of the first or last scanline are corrupted. Flag these
        cases to prevent that subsequent files are erroneously flagged as redundant.

        Args:
            max_length: Maximum length (minutes) for a file to be considered ok. Otherwise it
                        will be flagged as too long.
        """
        max_length = np.timedelta64(max_length, 'm')
        too_long = (df['end_time'] - df['start_time']) > max_length
        df.loc[too_long, 'global_quality_flag'] = QualityFlags.TOO_LONG

    def _set_global_qual_flags(self, df, platform):
        """Set global quality flags."""
        df = df.reset_index(drop=True)
        self._set_invalid_timestamp_flag(df, platform)
        self._set_too_short_flag(df)
        self._set_too_long_flag(df)
        self._set_redundant_flag(df)
        self._set_duplicate_flag(df)
        return df

    def _calc_overlap(self, df, open_end=False):
        """Compare timestamps of neighbouring files and determine overlap.

        For each file compare its timestamps with the start/end timestamps of the preceding and
        subsequent files. Determine the overlap-free part of the file and set the corresponding
        overlap_free_start/end attributes.
        """
        df_ok = df[df['global_quality_flag'] == QualityFlags.OK]

        for i in range(len(df_ok)):
            this_row = df_ok.iloc[i]
            prev_row = df_ok.iloc[i - 1] if i > 0 else None
            next_row = df_ok.iloc[i + 1] if i < len(df_ok) - 1 else None
            LOG.debug('Computing overlap for {}'.format(this_row['filename']))
            this_time = xr.open_dataset(this_row['filename'])['acq_time']

            # Compute overlap with preceding file
            if prev_row is not None:
                if prev_row['end_time'] >= this_row['start_time']:
                    prev_end_time = prev_row['end_time'].to_datetime64()
                    overlap_free_start = (this_time > prev_end_time).argmax().values
                else:
                    overlap_free_start = 0
                df.loc[df_ok.index[i], 'overlap_free_start'] = overlap_free_start
            else:
                # First file
                df.loc[df_ok.index[i], 'overlap_free_start'] = 0

            # Compute overlap with subsequent file
            if next_row is not None:
                if this_row['end_time'] >= next_row['start_time']:
                    next_start_time = next_row['start_time'].to_datetime64()
                    overlap_free_end = (this_time >= next_start_time).argmax().values - 1
                else:
                    overlap_free_end = this_row['along_track'] - 1
                df.loc[df_ok.index[i], 'overlap_free_end'] = overlap_free_end
            elif not open_end:
                # Last file
                df.loc[df_ok.index[i], 'overlap_free_end'] = this_row['along_track'] - 1

        return df


def update_metadata(mda):
    """Add additional metadata to level 1c files."""
    # Since xarray cannot modify files in-place, use netCDF4 directly. See
    # https://github.com/pydata/xarray/issues/2029.
    for _, row in mda.iterrows():
        LOG.debug('Updating metadata in {}'.format(row['filename']))
        with netCDF4.Dataset(filename=row['filename'], mode='r+') as nc:
            nc_acq_time = nc.variables['acq_time']
            for mda in ADDITIONAL_METADATA:
                mda = mda.copy()
                var_name = mda.pop('name')
                fill_value = mda.pop('fill_value')

                # Create nc variable
                try:
                    nc_var = nc.createVariable(var_name, datatype=mda.pop('dtype'),
                                               fill_value=fill_value)
                except RuntimeError:
                    # Variable already there
                    nc_var = nc.variables[var_name]

                # Write data to nc variable. Since netCDF4 cannot handle NaN nor NaT, disable
                # auto-masking, and set null-data to fill value manually. Furthermore, match
                # timestamp encoding with the acq_time variable.
                data = row[var_name]
                nc_var.set_auto_mask(False)
                if pd.isnull(data):
                    data = fill_value
                elif isinstance(data, pd.Timestamp):
                    data = encode_cf_datetime(data, units=nc_acq_time.units,
                                              calendar=nc_acq_time.calendar)[0]
                nc_var[:] = data

                # Set attributes of nc variable
                for key, val in mda.items():
                    nc_var.setncattr(key, val)
