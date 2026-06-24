"""
Immich Memories Notify
======================
Sends daily memory notifications to all configured users.

Uses a chance-based trigger system: each event type has a configured
probability of firing at every notification window.

Usage:
    python -m notify                         # Run all notification windows
    python -m notify --test                  # Test mode (minimal delays, use any date)
    python -m notify --dry-run               # Show what would be sent without sending
    python -m notify --check-updates         # Check GitHub for new releases
    python -m notify --date 2024-06-15       # Run against a specific date
"""

import argparse
import logging
import random
import sys
import time
from datetime import date, datetime

from .config import (
    get_assets_sent_today,
    is_feature_ready,
    load_config,
    load_state,
    mark_feature_fired,
    save_state,
    setup_logging,
)
from .features.albums import prepare_album_notification
from .features.birthday import find_birthday_people, prepare_birthday_notification
from .features.collage import is_collage_day, process_collage_slot
from .features.memories import prepare_memory_notification
from .features.persons import prepare_person_notification
from .features.then_and_now import (
    find_then_and_now_candidate,
    prepare_then_and_now_notification,
)
from .features.trip import find_trip_candidate, prepare_trip_notification
from .immich import (
    fetch_memories,
    fetch_people,
    filter_todays_memories,
    get_top_persons,
    parse_memories,
)
from .ntfy import send_single_notification
from .utils import calculate_random_delay, with_retry


def try_fire_event(
    user,
    config,
    state,
    target_date,
    settings,
    config_data,
    assets_sent,
    top_persons,
    parsed,
    test_mode,
    dry_run,
    force,
    logger,
):
    """
    Decide which single event type to fire, build it, and send the notification.

    Weekly Collage check — if collage day, steals the notification slot
    (fired exclusively, no other event fires this window).
    Phase 1 — Determine eligibility and chance for each event type.
    Phase 2 — Weighted random selection (one winner).
    Phase 3 — Build and send only the winning event.

    Returns a list of successfully sent notifications (at most one item).
    """
    name = user["name"]
    ntfy_user = user.get("ntfy_username")
    ntfy_pass = user.get("ntfy_password")
    ntfy_auth = (ntfy_user, ntfy_pass) if ntfy_user and ntfy_pass else None
    click_base = config["immich"].get("external_url") or "https://my.immich.app"
    trigger_chances = settings.get("trigger_chances", {})

    # =====================================================================
    # Weekly Collage — special case: steals the notification slot on collage day
    # Only fires if it's collage day AND no other event was chosen.
    # =====================================================================
    weekly_collage_enabled = settings.get("weekly_collage_enabled", False)
    is_collage = weekly_collage_enabled and is_collage_day(settings, target_date)
    collage_ready = False
    if is_collage:
        collage_ready = test_mode or is_feature_ready(
            state, name, "last_collage_date", 1, target_date
        )

    # =====================================================================
    # Phase 1: Determine eligibility + chance for each event type
    # =====================================================================
    candidates = []  # list of (event_name, chance, eligible)

    # --- Birthday ---
    birthday_chance = trigger_chances.get("birthday", 1.0)
    birthday_enabled = settings.get("birthday_enabled", True)
    candidates.append(("birthday", birthday_chance, birthday_enabled))

    # --- Memory ---
    memory_chance = trigger_chances.get("memory", 0.0)
    candidates.append(("memory", memory_chance, memory_chance > 0 and parsed["years"]))

    # --- Person ---
    person_chance = trigger_chances.get("person", 0.0)
    candidates.append(("person", person_chance, person_chance > 0 and top_persons))

    # --- Album ---
    album_chance = trigger_chances.get("album", 0.0)
    user_album_names = user.get("album_names", [])
    candidates.append(("album", album_chance, album_chance > 0 and user_album_names))

    # --- Then & Now ---
    tan_chance = trigger_chances.get("then_and_now", 0.0)
    tan_enabled = settings.get("then_and_now_enabled", True)
    tan_cooldown = settings.get("then_and_now_cooldown_days", 7)
    tan_ready = test_mode or is_feature_ready(
        state, name, "last_tan_date", tan_cooldown, target_date
    )
    candidates.append(
        (
            "then_and_now",
            tan_chance,
            tan_enabled and tan_chance > 0 and parsed["years"] and tan_ready,
        )
    )

    # --- Trip Highlights ---
    trip_chance = trigger_chances.get("trip_highlights", 0.0)
    trip_enabled = settings.get("trip_highlights_enabled", True)
    trip_cooldown = settings.get("trip_highlights_cooldown_days", 7)
    trip_ready = test_mode or is_feature_ready(
        state, name, "last_trip_date", trip_cooldown, target_date
    )
    candidates.append(
        (
            "trip_highlights",
            trip_chance,
            trip_enabled and trip_chance > 0 and parsed["years"] and trip_ready,
        )
    )

    # Filter to eligible candidates with non-zero chance
    eligible = [
        (name, chance)
        for name, chance, eligible in candidates
        if eligible and chance > 0
    ]

    if not eligible:
        logger.info(f"  [{name}] No eligible events this window")
        return []

    # Log eligible events
    logger.info(f"  [{name}] Eligible events: {[n for n, _ in eligible]}")

    # =====================================================================
    # Phase 2: Weekly Collage check — steals the notification slot on collage day
    # If collage day, fire it exclusively (no other event fires this window).
    # =====================================================================
    if is_collage and collage_ready:
        logger.info(f"  [{name}] Collage day — stealing notification slot")
        collage_results = _fire_weekly_collage(
            user,
            config,
            state,
            target_date,
            settings,
            config_data,
            test_mode,
            dry_run,
            force,
            ntfy_auth,
            logger,
        )
        if collage_results:
            mark_feature_fired(state, name, "last_collage_date", target_date)
        return collage_results

    # =====================================================================
    # Phase 2b: Weighted random selection — pick ONE winner
    # =====================================================================
    event_names, weights = zip(*eligible)
    chosen = random.choices(event_names, weights=weights, k=1)[0]
    logger.info(f"  [{name}] Chose: {chosen}")

    # =====================================================================
    # Phase 3: Build and send only the winning event
    # =====================================================================
    results = []

    if chosen == "birthday":
        results = _fire_birthday(
            user,
            config,
            state,
            target_date,
            settings,
            config_data,
            assets_sent,
            ntfy_auth,
            test_mode,
            logger,
        )

    elif chosen == "memory":
        results = _fire_memory(
            user,
            config,
            state,
            target_date,
            settings,
            config_data,
            assets_sent,
            top_persons,
            parsed,
            ntfy_auth,
            test_mode,
            logger,
        )

    elif chosen == "person":
        results = _fire_person(
            user,
            config,
            state,
            target_date,
            settings,
            config_data,
            assets_sent,
            top_persons,
            ntfy_auth,
            test_mode,
            logger,
        )

    elif chosen == "album":
        results = _fire_album(
            user,
            config,
            state,
            target_date,
            settings,
            config_data,
            assets_sent,
            ntfy_auth,
            test_mode,
            logger,
        )

    elif chosen == "then_and_now":
        results = _fire_then_and_now(
            user,
            config,
            state,
            target_date,
            settings,
            config_data,
            assets_sent,
            top_persons,
            ntfy_auth,
            test_mode,
            logger,
        )

    elif chosen == "trip_highlights":
        results = _fire_trip_highlights(
            user,
            config,
            state,
            target_date,
            settings,
            config_data,
            assets_sent,
            ntfy_auth,
            test_mode,
            logger,
            click_base,
        )

    return results


# =============================================================================
# Phase 3 helper: Build + send for each event type
# =============================================================================


def _fire_birthday(
    user,
    config,
    state,
    target_date,
    settings,
    config_data,
    assets_sent,
    ntfy_auth,
    test_mode,
    logger,
):
    results = []
    name = user["name"]
    immich_url = config["immich"]["url"]
    api_key = user["immich_api_key"]
    try:
        birthday_people = find_birthday_people(
            immich_url=immich_url,
            api_key=api_key,
            target_date=target_date,
            logger=logger,
        )
        if birthday_people:
            person = random.choice(birthday_people)
            birthday_messages = config_data.get("birthday_messages", [])
            birthday_titles = config_data.get("birthday_titles", [])
            notification = prepare_birthday_notification(
                birthday_person=person,
                immich_url=immich_url,
                api_key=api_key,
                messages=birthday_messages,
                test_mode=test_mode,
                logger=logger,
                title_templates=birthday_titles,
                exclude_asset_ids=assets_sent,
                exclude_days=settings.get("exclude_recent_days", 30),
            )
            if notification:
                success = _send_notification(
                    user,
                    notification,
                    config,
                    state,
                    ntfy_auth,
                    logger,
                    assets_sent,
                    target_date,
                    test_mode,
                    is_birthday=True,
                )
                if success:
                    results.append(notification)
        else:
            logger.info(f"  [{name}] Birthday chosen but no birthday person found")
    except Exception as e:
        logger.warning(f"  [{name}] Birthday check failed: {e}")
    return results


def _fire_memory(
    user,
    config,
    state,
    target_date,
    settings,
    config_data,
    assets_sent,
    top_persons,
    parsed,
    ntfy_auth,
    test_mode,
    logger,
):
    results = []
    name = user["name"]
    immich_url = config["immich"]["url"]
    api_key = user["immich_api_key"]
    try:
        memory_messages = config_data.get("messages", [])
        memory_titles = config_data.get("memory_titles", [])
        video_messages = config_data.get("video_messages", [])
        notification = prepare_memory_notification(
            parsed=parsed,
            assets_sent=assets_sent,
            top_person_ids={p["id"] for p in top_persons},
            immich_url=immich_url,
            api_key=api_key,
            messages=memory_messages,
            test_mode=test_mode,
            logger=logger,
            settings=settings,
            video_messages=video_messages,
            target_date=target_date,
            title_templates=memory_titles,
        )
        if notification:
            success = _send_notification(
                user,
                notification,
                config,
                state,
                ntfy_auth,
                logger,
                assets_sent,
                target_date,
                test_mode,
            )
            if success:
                results.append(notification)
    except Exception as e:
        logger.warning(f"  [{name}] Memory notification failed: {e}")
    return results


def _fire_person(
    user,
    config,
    state,
    target_date,
    settings,
    config_data,
    assets_sent,
    top_persons,
    ntfy_auth,
    test_mode,
    logger,
):
    results = []
    name = user["name"]
    immich_url = config["immich"]["url"]
    api_key = user["immich_api_key"]
    try:
        person_messages = config_data.get("person_messages", [])
        person_titles = config_data.get("person_titles", [])
        video_person_messages = config_data.get("video_person_messages", [])
        notification = prepare_person_notification(
            top_persons=top_persons,
            assets_sent=assets_sent,
            immich_url=immich_url,
            api_key=api_key,
            exclude_days=settings.get("exclude_recent_days", 30),
            person_messages=person_messages,
            test_mode=test_mode,
            logger=logger,
            settings=settings,
            video_person_messages=video_person_messages,
            title_templates=person_titles,
        )
        if notification:
            success = _send_notification(
                user,
                notification,
                config,
                state,
                ntfy_auth,
                logger,
                assets_sent,
                target_date,
                test_mode,
            )
            if success:
                results.append(notification)
    except Exception as e:
        logger.warning(f"  [{name}] Person notification failed: {e}")
    return results


def _fire_album(
    user,
    config,
    state,
    target_date,
    settings,
    config_data,
    assets_sent,
    ntfy_auth,
    test_mode,
    logger,
):
    results = []
    name = user["name"]
    immich_url = config["immich"]["url"]
    api_key = user["immich_api_key"]
    user_album_names = user.get("album_names", [])
    try:
        album_messages = config_data.get("album_messages", [])
        album_titles = config_data.get("album_titles", [])
        video_album_messages = config_data.get("video_album_messages", [])
        notification = prepare_album_notification(
            album_names=user_album_names,
            assets_sent=assets_sent,
            immich_url=immich_url,
            api_key=api_key,
            album_messages=album_messages,
            test_mode=test_mode,
            logger=logger,
            settings=settings,
            video_album_messages=video_album_messages,
            title_templates=album_titles,
            target_date=target_date,
        )
        if notification:
            success = _send_notification(
                user,
                notification,
                config,
                state,
                ntfy_auth,
                logger,
                assets_sent,
                target_date,
                test_mode,
            )
            if success:
                results.append(notification)
    except Exception as e:
        logger.warning(f"  [{name}] Album notification failed: {e}")
    return results


def _fire_then_and_now(
    user,
    config,
    state,
    target_date,
    settings,
    config_data,
    assets_sent,
    top_persons,
    ntfy_auth,
    test_mode,
    logger,
):
    results = []
    name = user["name"]
    immich_url = config["immich"]["url"]
    api_key = user["immich_api_key"]
    try:
        tan_cooldown = settings.get("then_and_now_cooldown_days", 7)
        tan_min_gap = settings.get("then_and_now_min_gap", 3)
        year_range = settings.get("year_range", 5)
        tan_ready = settings.get("then_and_now_enabled", True) and (
            test_mode
            or is_feature_ready(state, name, "last_tan_date", tan_cooldown, target_date)
        )

        if tan_ready:
            logger.info(
                f"  [{name}] Then & Now: checking (cooldown: {tan_cooldown} days)"
            )
        else:
            logger.info(f"  [{name}] Then & Now: on cooldown")

        if tan_ready:
            user_tan_state = state.get("users", {}).get(name, {})
            used_persons = user_tan_state.get("tan_persons_used", [])
            used_pairs = user_tan_state.get("tan_pairs_used", [])
            candidate = find_then_and_now_candidate(
                immich_url=immich_url,
                api_key=api_key,
                top_persons=top_persons,
                target_date=target_date,
                min_gap=tan_min_gap,
                year_range=year_range,
                logger=logger,
                used_person_ids=used_persons,
                used_pairs=used_pairs,
            )
            if candidate:
                tan_messages = config_data.get("then_and_now_messages", [])
                tan_titles = config_data.get("then_and_now_titles", [])
                notification = prepare_then_and_now_notification(
                    candidate=candidate,
                    immich_url=immich_url,
                    api_key=api_key,
                    messages=tan_messages,
                    test_mode=test_mode,
                    logger=logger,
                    title_templates=tan_titles,
                )
                if notification:
                    thumbnail_override = notification.get("composite_image")
                    success = _send_notification(
                        user,
                        notification,
                        config,
                        state,
                        ntfy_auth,
                        logger,
                        assets_sent,
                        target_date,
                        test_mode,
                        thumbnail_override=thumbnail_override,
                    )
                    if success:
                        mark_feature_fired(state, name, "last_tan_date", target_date)
                        user_state = state.setdefault("users", {}).setdefault(name, {})
                        person_id = notification.get("person_id", "")
                        if person_id:
                            used = user_state.setdefault("tan_persons_used", [])
                            used.append(person_id)
                            user_state["tan_persons_used"] = used[-20:]
                        pair_key = notification.get("tan_pair_key", "")
                        if pair_key:
                            pairs = user_state.setdefault("tan_pairs_used", [])
                            pairs.append(pair_key)
                            user_state["tan_pairs_used"] = pairs[-50:]
                        results.append(notification)
            else:
                logger.info(f"  [{name}] Then & Now: chosen but no candidate found")
    except Exception as e:
        logger.warning(f"  [{name}] Then & Now lookup failed: {e}")
    return results


def _fire_trip_highlights(
    user,
    config,
    state,
    target_date,
    settings,
    config_data,
    assets_sent,
    ntfy_auth,
    test_mode,
    logger,
    click_base,
):
    results = []
    name = user["name"]
    immich_url = config["immich"]["url"]
    api_key = user["immich_api_key"]
    try:
        trip_cooldown = settings.get("trip_highlights_cooldown_days", 7)
        trip_min_photos = settings.get("trip_highlights_min_photos", 5)
        year_range = settings.get("year_range", 5)
        trip_ready = settings.get("trip_highlights_enabled", True) and (
            test_mode
            or is_feature_ready(
                state, name, "last_trip_date", trip_cooldown, target_date
            )
        )

        if trip_ready:
            logger.info(
                f"  [{name}] Trip Highlights: checking (cooldown: {trip_cooldown} days)"
            )
        else:
            logger.info(f"  [{name}] Trip Highlights: on cooldown")

        if trip_ready:
            home_cities = user.get("home_cities") or (
                [user["home_city"]] if user.get("home_city") else []
            )
            trip = find_trip_candidate(
                immich_url=immich_url,
                api_key=api_key,
                target_date=target_date,
                home_cities=home_cities,
                min_photos=trip_min_photos,
                year_range=year_range,
                logger=logger,
            )
            if trip:
                trip_messages = config_data.get("trip_highlights_messages", [])
                trip_titles = config_data.get("trip_highlights_titles", [])
                notification = prepare_trip_notification(
                    trip=trip,
                    immich_url=immich_url,
                    api_key=api_key,
                    messages=trip_messages,
                    test_mode=test_mode,
                    logger=logger,
                    title_templates=trip_titles,
                    click_base=click_base,
                )
                if notification:
                    success = _send_notification(
                        user,
                        notification,
                        config,
                        state,
                        ntfy_auth,
                        logger,
                        assets_sent,
                        target_date,
                        test_mode,
                        thumbnail_override=notification.get("collage_data"),
                    )
                    if success:
                        mark_feature_fired(state, name, "last_trip_date", target_date)
                        results.append(notification)
            else:
                logger.info(
                    f"  [{name}] Trip Highlights: chosen but no candidate found"
                )
    except Exception as e:
        logger.warning(f"  [{name}] Trip Highlights failed: {e}")
    return results


def _fire_weekly_collage(
    user,
    config,
    state,
    target_date,
    settings,
    config_data,
    test_mode,
    dry_run,
    force,
    ntfy_auth,
    logger,
):
    results = []
    name = user["name"]
    try:
        if is_collage_day(settings, target_date):
            logger.info(f"  [{name}] Collage: YES (weekly collage day)")
            result = process_collage_slot(
                user=user,
                config=config,
                state=state,
                target_date=target_date,
                test_mode=test_mode,
                dry_run=dry_run,
                force=force,
                logger=logger,
            )
            if result.get("success"):
                collage_notification = {
                    "title": "[Collage] Sent",
                    "has_content": True,
                }
                results.append(collage_notification)
        else:
            logger.info(f"  [{name}] Collage: NO (not collage day)")
    except Exception as e:
        logger.warning(f"  [{name}] Collage generation failed: {e}")
    return results


def _send_notification(
    user,
    notification,
    config,
    state,
    ntfy_auth,
    logger,
    assets_sent,
    target_date,
    test_mode,
    thumbnail_override=None,
    is_birthday=False,
):
    """Send a notification and update state."""
    name = user["name"]
    result = {"success": True, "name": name, "asset_id": None}

    if not notification or not notification.get("has_content"):
        return False

    if not assets_sent:
        from .config import get_assets_sent_today

        assets_sent = get_assets_sent_today({}, name, target_date)

    success = send_single_notification(
        user=user,
        notification=notification,
        config=config,
        ntfy_auth=ntfy_auth,
        logger=logger,
        thumbnail_override=thumbnail_override,
    )

    if success:
        logger.info(f"  [{name}] Notification sent!")
        result["asset_id"] = notification.get("asset_id")
        asset_id = notification.get("asset_id")
        if asset_id and asset_id not in assets_sent:
            if "assets_sent_today" not in state.get("users", {}).get(name, {}):
                state.setdefault("users", {}).setdefault(name, {})
                state["users"][name]["assets_sent_today"] = []
            state["users"][name]["assets_sent_today"].append(asset_id)
        if is_birthday:
            mark_feature_fired(state, name, "last_birthday_date", target_date)
    else:
        result["success"] = False

    return success


def main():
    parser = argparse.ArgumentParser(
        description="Send Immich memory notifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m notify --window-duration 60              # Single run with 60-minute window
  python -m notify --test --window-duration 60       # Test mode
  python -m notify --dry-run --window-duration 60    # Preview without sending
  python -m notify --date 2024-06-15 --window-duration 60  # Specific date
        """,
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument(
        "--check-updates", action="store_true", help="Check for new releases on GitHub"
    )
    parser.add_argument(
        "--test", action="store_true", help="Test mode (minimal delays, use any date)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be sent"
    )
    parser.add_argument(
        "--no-delay",
        action="store_true",
        help="Skip random delays between notifications",
    )
    parser.add_argument("--date", help="Specific date to check (YYYY-MM-DD)")
    parser.add_argument(
        "--window-duration",
        type=int,
        default=None,
        help="Window duration in minutes (set by crontab)",
    )
    args = parser.parse_args()

    if args.check_updates:
        from .update_check import check_for_updates

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        logger = logging.getLogger("immich-memories-notify")
        return check_for_updates(config_path=args.config, logger=logger)

    # Load config first to get log settings
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"Error loading config: {e}")
        return 1

    # Setup logging
    settings = config.get("settings", {})
    logger = setup_logging(
        level=settings.get("log_level", "INFO"),
        log_file=settings.get("log_file"),
    )

    # Determine target date
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"Invalid date format: {args.date} (use YYYY-MM-DD)")
            return 1
    else:
        target_date = date.today()

    logger.info("=" * 60)
    logger.info("Immich Memories Notify")
    logger.info("=" * 60)
    logger.info(f"Date:    {target_date}")
    logger.info(f"Config:  {args.config}")

    if args.test:
        logger.info("Mode:    TEST")
    if args.dry_run:
        logger.info("Mode:    DRY RUN")

    if args.window_duration:
        logger.info(f"Window Duration: {args.window_duration} minutes")

    # Load state
    state_file = settings.get("state_file", "state/state.json")
    state = load_state(state_file)

    # Get enabled users
    users = [u for u in config.get("users", []) if u.get("enabled", True)]
    logger.info(f"Users:   {len(users)}")

    if not users:
        logger.warning("No enabled users found in config")
        return 0

    # Check if today is collage day
    collage_day = is_collage_day(settings, target_date)
    if collage_day:
        logger.info("Collage day: YES")

    total_success = 0
    total_users = len(users)

    logger.info("-" * 60)
    logger.info("Starting notification processing")

    # Calculate and apply random delay based on window duration
    if args.window_duration and not args.no_delay and not args.dry_run and not args.test:
        delay_seconds = random.randint(0, args.window_duration * 60)
        if delay_seconds > 0:
            delay_minutes = delay_seconds // 60
            logger.info(f"Delay: ~{delay_minutes} minutes (window: {args.window_duration} min)")
            time.sleep(delay_seconds)

    for user in users:
        user_name = user["name"]
        api_key = user["immich_api_key"]
        if not api_key:
            logger.error(f"  [{user_name}] No API key configured")
            continue

        # Fetch memories with retry
        try:
            memories = with_retry(
                lambda immich_url=config["immich"]["url"], api_key=api_key: (
                    fetch_memories(immich_url, api_key)
                ),
                max_attempts=settings["retry"]["max_attempts"],
                delay=settings["retry"]["delay_seconds"],
                logger=logger,
            )
        except Exception as e:
            logger.error(f"  [{user_name}] Failed to fetch memories: {e}")
            continue

        # Filter for today
        todays = filter_todays_memories(memories, target_date)

        # In test mode, find any date with memories
        if args.test and not todays:
            for memory in memories[:10]:
                show_at = memory.get("showAt", "")
                if show_at:
                    test_date = datetime.strptime(show_at[:10], "%Y-%m-%d").date()
                    todays = filter_todays_memories(memories, test_date)
                    if todays:
                        logger.info(
                            f"  [{user_name}] Test mode: using date {test_date}"
                        )
                        break

        # Parse memories by year
        parsed = (
            parse_memories(todays, config["immich"]["url"], api_key)
            if todays
            else {"years": [], "by_year": {}}
        )

        # Fetch top persons
        try:
            top_persons = get_top_persons(
                config["immich"]["url"],
                api_key,
                limit=settings.get("top_persons_limit", 5),
                logger=logger,
            )
        except Exception as e:
            logger.warning(f"  [{user_name}] Could not fetch top persons: {e}")
            top_persons = []

        # Try to fire events based on chances
        results = try_fire_event(
            user=user,
            config=config,
            state=state,
            target_date=target_date,
            settings=settings,
            config_data=config,
            assets_sent=get_assets_sent_today(state, user_name, target_date),
            top_persons=top_persons,
            parsed=parsed,
            test_mode=args.test,
            dry_run=args.dry_run,
            force=False,
            logger=logger,
        )

        if results:
            logger.info(
                f"  [{user_name}] Sent {len(results)} notification(s)"
            )
            total_success += 1

        # Save state after each user to avoid losing progress on crash
        if not args.dry_run:
            save_state(state_file, state)

    logger.info(f"{total_success}/{total_users} users received notifications")
    logger.info("=" * 60)
    logger.info("Complete")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
