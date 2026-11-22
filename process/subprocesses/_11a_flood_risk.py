"""Import configured flood prone areas and derive sample-point flood risk.

The flood risk indicator measures the share of the population whose sample
point lies within the configured buffer distance of a mapped flood prone
area.  Flood prone area data are supplied as a spatial file (for example a
GeoPackage layer) in the study region configuration under ``flood``.
"""

import os
import sys
import time

import geopandas as gpd
import ghsci
from script_running_log import script_running_log
from sqlalchemy import text

DEFAULT_BUFFER_DISTANCE = 100


def flood_configuration(r):
    """Return the normalised flood configuration for a study region."""
    flood = r.config.get('flood')
    if not flood:
        return None
    if isinstance(flood, str):
        flood = {'data': flood}
    flood = dict(flood)
    if not flood.get('data'):
        return None
    flood.setdefault('buffer_distance', DEFAULT_BUFFER_DISTANCE)
    flood['buffer_distance'] = float(flood['buffer_distance'])
    return flood


def _resolve_data_path(data):
    """Split an optional ``path:layer`` specification into path and layer."""
    if os.path.exists(data):
        return data, None
    candidate, _, layer = data.rpartition(':')
    if candidate and os.path.exists(candidate):
        return candidate, layer
    return data, None


def import_flood_areas(r, flood):
    """Read the configured flood prone areas and store them in PostGIS."""
    path, layer = _resolve_data_path(flood['data'])
    print(f'  Reading flood prone areas from {os.path.basename(path)}...')
    areas = gpd.read_file(path, layer=layer)
    if areas.empty:
        raise ValueError(
            f"The configured flood data for {r.codename} contain no features: "
            f'{flood["data"]}',
        )
    target_srid = r.config['crs']['srid']
    if areas.crs is None:
        areas = areas.set_crs(epsg=target_srid)
    elif areas.crs.to_epsg() != target_srid:
        areas = areas.to_crs(epsg=target_srid)
    try:
        areas = areas.set_geometry(areas.geometry.make_valid())
    except AttributeError:
        areas = areas.set_geometry(areas.geometry.buffer(0))
    areas = areas[['geometry']].rename_geometry('geom')
    areas = areas[~areas.geometry.is_empty & areas.geometry.notna()].copy()
    print(f'  {len(areas):,} flood prone area features')
    with r.engine.begin() as connection:
        connection.execute(text('DROP TABLE IF EXISTS flood_prone_areas;'))
        areas.to_postgis(
            'flood_prone_areas',
            connection,
            index=False,
            if_exists='replace',
        )
        connection.execute(
            text(
                'CREATE INDEX IF NOT EXISTS flood_prone_areas_geom_idx '
                'ON flood_prone_areas USING GIST (geom);',
            ),
        )
    return areas


def calculate_flood_risk(r, flood):
    """Flag sample points within the configured buffer of a flood prone area."""
    distance = flood['buffer_distance']
    point_summary = r.config['point_summary']
    with r.engine.begin() as connection:
        connection.execute(
            text(
                f'ALTER TABLE {point_summary} '
                'ADD COLUMN IF NOT EXISTS sp_flood_risk_score double precision;',
            ),
        )
        print(
            f'  Flagging sample points within {distance:g} m of flood prone '
            'areas...',
        )
        connection.execute(
            text(
                f"""
                UPDATE {point_summary} p
                SET sp_flood_risk_score = CASE WHEN EXISTS (
                    SELECT 1
                    FROM flood_prone_areas f
                    WHERE ST_DWithin(p.geom, f.geom, :distance)
                ) THEN 1 ELSE 0 END;
                """,
            ),
            {'distance': distance},
        )
        total, within = connection.execute(
            text(
                f"""
                SELECT count(*),
                       count(*) FILTER (WHERE sp_flood_risk_score >= 1)
                FROM {point_summary};
                """,
            ),
        ).one()
    if total:
        print(
            f'  {within:,} of {total:,} sample points ({within / total:.1%}) '
            'are within the flood buffer',
        )
    return {'sample_points': total, 'within_buffer': within}


def flood_risk(codename):
    """Import flood prone areas and calculate sample-point flood risk."""
    start = time.time()
    script = '_11a_flood_risk'
    task = 'Analyse flood risk for sample points'
    r = ghsci.Region(codename)
    flood = flood_configuration(r)
    if flood is None:
        print(
            f'\nNo flood data configured for {r.codename}; skipping flood risk analysis.',
        )
        # The flood indicator is optional, but downstream aggregation expects
        # every configured sample point variable to be present, retaining an
        # all-null column where an optional analysis was skipped (as happens
        # for a region without GTFS data).  Add the column as null so the
        # sample point schema remains consistent.
        if r.config['point_summary'] in r.tables:
            with r.engine.begin() as connection:
                connection.execute(
                    text(
                        f'ALTER TABLE {r.config["point_summary"]} '
                        'ADD COLUMN IF NOT EXISTS sp_flood_risk_score '
                        'double precision;',
                    ),
                )
        script_running_log(r.config, script, task, start)
        r.engine.dispose()
        return None
    print(f'\nFlood risk for {r.codename}')
    import_flood_areas(r, flood)
    calculate_flood_risk(r, flood)
    script_running_log(r.config, script, task, start)
    r.engine.dispose()
    return None


def main():
    try:
        codename = sys.argv[1]
    except IndexError:
        codename = None
    flood_risk(codename)


if __name__ == '__main__':
    main()
