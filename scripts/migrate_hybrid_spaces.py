#!/usr/bin/env python3
"""Idempotent atomic migration script for multi-user hybrid spaces."""

from __future__ import annotations

# ruff: noqa: E501
import argparse
import logging
import sys
from pathlib import Path

import frontmatter

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("migrate_hybrid_spaces")

MIGRATIONS: list[dict[str, str | None]] = [
    # 3.2 FIX DE PRIVACIDAD URGENTE - 6 notas clínicas de Mercedes de work a personal-mercedes
    {
        "source": "work/Tasks/logging-bronchitis-episode-health-status-and-semantic-memory-for-mercedes-da302086.md",
        "target_space": "personal-mercedes",
        "owner": "mercedes",
    },
    {
        "source": "work/Decisions/post-corticosteroid-return-protocol-and-clinical-interpretation-for-mercedes-60ff038b.md",
        "target_space": "personal-mercedes",
        "owner": "mercedes",
    },
    {
        "source": "work/Tasks/save-semantic-memory-and-sync-garmin-data-for-mercedes-54d61e25.md",
        "target_space": "personal-mercedes",
        "owner": "mercedes",
    },
    {
        "source": "work/Tasks/sync-and-analyze-swim-session-for-mercedes-7028d98a.md",
        "target_space": "personal-mercedes",
        "owner": "mercedes",
    },
    {
        "source": "work/Tasks/garmin-sync-and-biometric-analysis-for-user-mercedes-d98cb5c8.md",
        "target_space": "personal-mercedes",
        "owner": "mercedes",
    },
    {
        "source": "work/Tasks/physiological-recovery-analysis-and-return-to-activity-criteria-93fca860.md",
        "target_space": "personal-mercedes",
        "owner": "mercedes",
    },
    # 3.3 MIGRACIÓN RESTO: A personal-mercedes
    {
        "source": "personal/Tasks/mensaje-personal-programado-para-fsirio-sin-conocimiento-de-ingenier-a-e72dc7da.md",
        "target_space": "personal-mercedes",
        "owner": "mercedes",
    },
    # 3.3 MIGRACIÓN RESTO: A shared
    {
        "source": "personal/Tasks/service-honda-fit-kee-116-16-09-2026-221-348-km-a317b5d1.md",
        "target_space": "shared",
        "owner": None,
    },
    {
        "source": "personal/Tasks/honda-fit-kee-116-service-at-221-348-km-2026-09-16-1bec9f26.md",
        "target_space": "shared",
        "owner": None,
    },
    {
        "source": "personal/Proactive_Intents/recordatorio-service-del-auto-en-hondafc-mi-rcoles-16-09-8am-8c0ce294.md",
        "target_space": "shared",
        "owner": None,
    },
    {
        "source": "personal/Repositorys/inventario-de-medicamentos-caj-n-50b86ae9.md",
        "target_space": "shared",
        "owner": None,
    },
    {
        "source": "personal/Proactive_Intents/prueba-asado-domingo-0b27438c.md",
        "target_space": "shared",
        "owner": None,
    },
    {
        "source": "personal/Proactive_Intents/aviso-clima-ma-ana-evento-mercedes-salida-8am-de68f1f0.md",
        "target_space": "shared",
        "owner": None,
    },
    {
        "source": "personal/Systems/evento-fiesta-del-salame-en-mercedes-buenos-aires-13-09-2026-a12d1266.md",
        "target_space": "shared",
        "owner": None,
    },
    # 3.3 MIGRACIÓN RESTO: A personal-fsirio
    {
        "source": "personal/Patterns/jargon-gap-vs-knowledge-gap-vocabulary-mapping-strategy-for-technical-interviews-b72a04b6.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Tasks/situaci-n-laboral-negociando-puesto-de-6-meses-en-otra-consultora-88bdaca7.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Tasks/situaci-n-laboral-en-bench-en-dataart-desde-04-09-2026-270d60d4.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Tasks/dataart-interview-preparation-and-reminders-3d93cc9d.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Projects/gcp-implementation-details-for-biometric-ai-platform-185bfae1.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Projects/inventario-de-cuchillos-edc-b-ker-r-plica-trento-1a451e1f.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Projects/gcp-engineering-experience-and-interview-prep-for-dataart-6648c0db.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Projects/edc-knife-inventory-update-b-ker-in-car-trento-camping-knife-f5f14451.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Projects/cv-completo-federico-alejandro-sirio-perfil-profesional-9b565144.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Projects/engineering-profile-biometric-ai-platform-homelab-and-exocortex-brain-5c4a90b0.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Proactive_Intents/kickoff-full-day-de-estudio-finalsite-sre-7d5d3c68.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Proactive_Intents/pre-entrevista-finalsite-sre-2h-antes-248685fc.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Systems/cv-experience-early-networking-linux-and-cloud-certifications-federico-alejandro-sirio-part-2-0a96f86e.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
    {
        "source": "personal/Systems/perfil-maestro-fsirio-dbbdefc2.md",
        "target_space": "personal-fsirio",
        "owner": "fsirio",
    },
]


def migrate(vault_dir: Path, dry_run: bool = False) -> int:
    """Execute migration atomically and idempotently."""
    migrated_count = 0
    already_correct_count = 0

    for item in MIGRATIONS:
        src_rel = item["source"]
        target_space = item["target_space"]
        owner = item["owner"]

        assert isinstance(src_rel, str)
        assert isinstance(target_space, str)

        src_path = vault_dir / src_rel
        category = src_rel.split("/")[1]
        filename = src_rel.split("/")[2]
        dest_path = vault_dir / target_space / category / filename

        # Case 1: Destination file already exists
        if dest_path.exists():
            post = frontmatter.load(dest_path)
            current_space = post.metadata.get("space_id")
            current_owner = post.metadata.get("owner")

            needs_update = (current_space != target_space) or (current_owner != owner)
            if not needs_update:
                logger.info(
                    "Already at destination and correct: %s",
                    dest_path.relative_to(vault_dir),
                )
                already_correct_count += 1
                if src_path.exists() and src_path != dest_path:
                    logger.info(
                        "Cleaning up duplicate source file: %s",
                        src_path.relative_to(vault_dir),
                    )
                    if not dry_run:
                        src_path.unlink()
                continue
            else:
                logger.info(
                    "Updating existing destination file metadata: %s",
                    dest_path.relative_to(vault_dir),
                )
                if not dry_run:
                    post.metadata["space_id"] = target_space
                    if owner is not None:
                        post.metadata["owner"] = owner
                    elif "owner" in post.metadata:
                        post.metadata["owner"] = None
                    tmp_dest = dest_path.with_suffix(".md.tmp")
                    tmp_dest.write_text(frontmatter.dumps(post), encoding="utf-8")
                    tmp_dest.replace(dest_path)
                    if src_path.exists() and src_path != dest_path:
                        src_path.unlink()
                migrated_count += 1
                continue

        # Case 2: Source file exists
        if not src_path.exists():
            logger.warning("Neither source nor destination exists: %s", src_rel)
            continue

        post = frontmatter.load(src_path)
        post.metadata["space_id"] = target_space
        if owner is not None:
            post.metadata["owner"] = owner
        elif "owner" in post.metadata:
            post.metadata["owner"] = None

        logger.info(
            "%s: %s -> %s (owner=%s)",
            "[DRY-RUN] Move" if dry_run else "Move",
            src_rel,
            dest_path.relative_to(vault_dir),
            owner,
        )

        if not dry_run:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_dest = dest_path.with_suffix(".md.tmp")
            tmp_dest.write_text(frontmatter.dumps(post), encoding="utf-8")
            tmp_dest.replace(dest_path)
            src_path.unlink()

        migrated_count += 1

    # Cleanup empty directories in personal/ if all files moved
    personal_dir = vault_dir / "personal"
    if personal_dir.exists() and not dry_run:
        remaining = list(personal_dir.rglob("*.md"))
        if not remaining:
            logger.info("Cleaning up empty personal/ directory tree...")
            import shutil

            shutil.rmtree(personal_dir)

    logger.info(
        "Migration summary: %d migrated/updated, %d already correct. Total handled: %d.",
        migrated_count,
        already_correct_count,
        migrated_count + already_correct_count,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate Exocortex notes to hybrid spaces."
    )
    parser.add_argument(
        "--vault-dir",
        type=Path,
        default=Path("/home/fsirio/homelab/exocortex/data/Vault"),
        help="Path to the Vault directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate migration without modifying files",
    )
    args = parser.parse_args()
    return migrate(args.vault_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
