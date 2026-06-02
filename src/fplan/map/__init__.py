"""Map artifact domain logic: headless extraction, clustering, and I/O.

Split from the thin ``fplan.cli.map`` command module: ``extract`` drives
Factorio, ``cluster`` post-processes the raw dump, and ``artifact`` reads /
writes / summarizes the resulting YAML.
"""
