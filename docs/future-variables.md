# Deferred source layers

The first product includes all fourteen properties at the six standard depths,
plus HSG on its own native grid. It excludes the published topsoil (0-30 cm)
and subsoil (30-200 cm) aggregates.

Adding aggregates requires a separately modeled overlapping depth coordinate or
separate groups, an inventory of their actual filenames/grid/dtype/nodata, and
validation against their own published rasters. Do not simply append them to the
six-interval coordinate or recreate them as ordinary means of the depth layers.

The report describes property-specific aggregation: thickness-weighted means,
pedotransfer functions evaluated on aggregated inputs, and a harmonic treatment
for conductivity. STC is categorical. The source README swaps the aggregate
topsoil/subsoil labels; use the numerical intervals and the report's definitions.

Reprojection onto the sibling SoilGrids Icechunk grid is also deferred. HiHydroSoil
uses EPSG:4326, while that store uses Interrupted Goode Homolosine. A common grid
requires an explicit resampling policy and produces a derived dataset rather
than preserving native source pixels.
