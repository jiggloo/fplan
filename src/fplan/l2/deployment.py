"""L2 deployment overlay — applies per-building deployment to a Facility.

This is the L2 side of the stage-enrichment inversion (see docs/): the base
``model.make_facility`` stays dependency-free and returns a bare Facility;
*here* — in L2, which depends downward on the model — we overlay the persistent
infrastructure and tile footprint from the L2 config's deployment registry.

The registry data lives in the tunable L2 config (:mod:`fplan.l2.config`,
populated from ``resources/l2-defaults.yaml``), so deployment packings are a
power-user knob. This module is just the function that reads a pattern and
stamps it onto a Facility.

``DeploymentPattern`` is re-exported from :mod:`fplan.l2.config` for callers
that referenced it here.
"""

from __future__ import annotations

from dataclasses import replace

from fplan.l2.config import DeploymentPattern, L2Config
from fplan.model import Building, Facility, GameModel

__all__ = ["DeploymentPattern", "deployed_facility"]


def deployed_facility(
    model: GameModel, building: Building, config: L2Config
) -> Facility:
    """A Facility with the config's deployment pattern overlaid.

    Calls the base (deployment-free) ``model.make_facility`` and applies the
    building's registered ``infrastructure_items`` and ``tile_footprint``. An
    unregistered building keeps the bare prototype footprint (so it still counts
    against the total-area cap) but gets no infrastructure reservation and no
    per-resource tile-pool cap — matching pre-deployment behavior on the
    infra/per-resource dimensions.
    """
    base = model.make_facility(building)
    pattern = config.deployment_for(building.name)
    return replace(
        base,
        infrastructure_items=dict(pattern.infrastructure_items),
        tile_footprint=(
            pattern.tile_footprint
            if pattern.tile_footprint > 0
            else base.tile_footprint
        ),
    )
