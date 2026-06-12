import unittest
from datetime import datetime, timedelta

from health_sync import (
    GOOGLE_FIT_PACKAGE,
    KS_FIT_PACKAGE,
    _attach_googlefit_routes,
    _choose_yazio_consumed_items,
    _format_manual_note_entries,
    _healthconnect_training_type,
    _iter_lan_scan_candidates,
    _merge_workouts,
    _merge_yazio_feelings_archive,
    _parse_adb_device_serials,
    _parse_googlefit_minimap_cache_row,
    _parse_googlefit_session_location_cache_row,
    _parse_yazio_done_trainings,
)
from build_dashboard import (
    aggregate_sleep_by_date,
    compute_energy_balance,
    compute_energy_rollups,
    compute_food_ideas,
    compute_notes_status,
    compute_nutrition_diary,
    compute_section_kpis,
    compute_sync_notice,
    format_chart_data,
    normalize_food_profile,
)


class DashboardLogicTests(unittest.TestCase):
    @staticmethod
    def _pb_varint(value):
        out = bytearray()
        value = int(value)
        while value >= 0x80:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value)
        return bytes(out)

    @classmethod
    def _gf_location_point(cls, lat, lon, altitude, accuracy, ts_ms):
        import struct

        msg = bytearray()
        msg.extend(b"\x09")
        msg.extend(struct.pack("<d", lat))
        msg.extend(b"\x11")
        msg.extend(struct.pack("<d", lon))
        msg.extend(b"\x19")
        msg.extend(struct.pack("<d", altitude))
        msg.extend(b"\x25")
        msg.extend(struct.pack("<f", accuracy))
        msg.extend(b"\x28")
        msg.extend(cls._pb_varint(ts_ms))
        return b"\x0a" + cls._pb_varint(len(msg)) + bytes(msg)

    @classmethod
    def _gf_session_location_request(cls, start_ms, end_ms):
        return (
            b"\x08" + cls._pb_varint(start_ms)
            + b"\x10" + cls._pb_varint(end_ms)
        )

    @classmethod
    def _gf_minimap_request(cls, session_id):
        raw = session_id.encode("utf-8")
        return b"\x0a" + cls._pb_varint(len(raw)) + raw

    def test_googlefit_session_location_cache_row_decodes_route_points(self):
        start_ms = 1_779_378_513_079
        end_ms = 1_779_385_716_643
        request = self._gf_session_location_request(start_ms, end_ms)
        response = b"".join([
            self._gf_location_point(50.9089355, 34.7774467, 161.3, 20.1, start_ms + 8_000),
            self._gf_location_point(50.9091000, 34.7780000, 162.1, 18.0, start_ms + 18_000),
        ]) + b"\x10\x01"

        route = _parse_googlefit_session_location_cache_row(request, response)

        self.assertIsNotNone(route)
        self.assertEqual(route["start_ms"], start_ms)
        self.assertEqual(route["end_ms"], end_ms)
        self.assertEqual(len(route["points"]), 2)
        self.assertAlmostEqual(route["points"][0]["lat"], 50.9089355, places=5)
        self.assertAlmostEqual(route["points"][0]["lon"], 34.7774467, places=5)

    def test_googlefit_minimap_cache_row_decodes_real_route_preview(self):
        start_ms = 1_778_931_582_655
        session_id = f"4798dfff3a9a1bc6:watch-activemode:walking:{start_ms}"
        request = self._gf_minimap_request(session_id)
        response = b"".join([
            self._gf_location_point(50.9126854, 34.7777748, 158.2, 15.9, start_ms + 5_000),
            self._gf_location_point(50.9119000, 34.7792000, 160.0, 12.0, start_ms + 65_000),
            self._gf_location_point(50.9090270, 34.7777940, 161.3, 11.5, start_ms + 120_000),
        ])

        route = _parse_googlefit_minimap_cache_row(request, response)

        self.assertIsNotNone(route)
        self.assertEqual(route["start_ms"], start_ms)
        self.assertEqual(route["route_source"], "Google Fit minimap")
        self.assertEqual(len(route["points"]), 3)
        self.assertAlmostEqual(route["points"][0]["lat"], 50.9126854, places=5)

    def test_googlefit_routes_attach_only_to_matching_outdoor_fit_workout(self):
        start_ms = 1_779_378_513_079
        end_ms = 1_779_385_716_643
        routes = [{
            "start_ms": start_ms,
            "end_ms": end_ms,
            "points": [
                {"lat": 50.9089355, "lon": 34.7774467, "ts_ms": start_ms + 8_000},
                {"lat": 50.9091000, "lon": 34.7780000, "ts_ms": start_ms + 18_000},
            ],
        }]
        workouts = [
            {
                "id": "hc_251",
                "training": "walking",
                "source_package": GOOGLE_FIT_PACKAGE,
                "_start_ms": start_ms,
                "_end_ms": end_ms,
            },
            {
                "id": "hc_252",
                "training": "walking_treadmill",
                "source_package": KS_FIT_PACKAGE,
                "_start_ms": start_ms,
                "_end_ms": end_ms,
            },
        ]

        _attach_googlefit_routes(workouts, routes)

        self.assertEqual(len(workouts[0]["route_points"]), 2)
        self.assertEqual(workouts[0]["route_source"], "Google Fit")
        self.assertNotIn("route_points", workouts[1])

    def test_googlefit_minimap_route_must_be_distance_plausible(self):
        start_ms = 1_779_325_489_400
        end_ms = start_ms + 180_000
        routes = [{
            "start_ms": start_ms,
            "end_ms": end_ms,
            "route_source": "Google Fit minimap",
            "points": [
                {"lat": -12.048279, "lon": -77.048431, "ts_ms": start_ms + 8_000},
                {"lat": -12.031227, "lon": -77.045380, "ts_ms": start_ms + 120_000},
            ],
        }]
        workouts = [{
            "id": "hc_short_walk",
            "training": "walking",
            "source_package": GOOGLE_FIT_PACKAGE,
            "distance_km": 0.2,
            "_start_ms": start_ms,
            "_end_ms": end_ms,
        }]

        _attach_googlefit_routes(workouts, routes)

        self.assertNotIn("route_points", workouts[0])

    def test_parse_adb_device_serials_ignores_offline_transports(self):
        out = """List of devices attached
4c75020d               offline transport_id:168
192.168.50.72:5555     device product:demo model:example device:demo transport_id:117
"""

        self.assertEqual(_parse_adb_device_serials(out), ["192.168.50.72:5555"])

    def test_lan_scan_candidates_use_unique_24_subnets(self):
        candidates = list(_iter_lan_scan_candidates(["192.168.50.72", "192.168.50.224", "127.0.0.1", "bad"]))

        self.assertIn("192.168.50.1", candidates)
        self.assertIn("192.168.50.254", candidates)
        self.assertNotIn("127.0.0.1", candidates)
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_sleep_segments_are_merged_into_one_night(self):
        sessions = [
            {
                "date": "2026-04-20",
                "bedtime": "12:14",
                "waketime": "14:14",
                "duration_min": 120.9,
                "stages": {
                    "deep": {"min": 5, "pct": 4},
                    "rem": {"min": 20, "pct": 17},
                    "light": {"min": 90, "pct": 74},
                    "awake": {"min": 5.9, "pct": 5},
                },
                "heart_rate": {"avg": 71},
            },
            {
                "date": "2026-04-20",
                "bedtime": "11:26",
                "waketime": "12:01",
                "duration_min": 34.6,
                "stages": {
                    "deep": {"min": 1, "pct": 3},
                    "rem": {"min": 3, "pct": 9},
                    "light": {"min": 29, "pct": 84},
                    "awake": {"min": 1.6, "pct": 4},
                },
                "heart_rate": {"avg": 73},
            },
            {
                "date": "2026-04-20",
                "bedtime": "05:40",
                "waketime": "10:35",
                "duration_min": 295.0,
                "stages": {
                    "deep": {"min": 30, "pct": 10},
                    "rem": {"min": 60, "pct": 20},
                    "light": {"min": 190, "pct": 64},
                    "awake": {"min": 15, "pct": 5},
                },
                "heart_rate": {"avg": 69},
            },
        ]

        merged = aggregate_sleep_by_date(sessions)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["date"], "2026-04-20")
        self.assertAlmostEqual(merged[0]["duration_min"], 450.5)
        self.assertEqual(merged[0]["bedtime"], "05:40")
        self.assertEqual(merged[0]["waketime"], "14:14")

    def test_food_ideas_prefer_useful_meals_over_random_stale_snacks(self):
        data = {
            "nutrition_goals": {"protein_g": 150},
            "nutrition": [
                {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "total_kcal": 2600,
                    "protein_g": 45,
                    "fat_g": 130,
                    "carb_g": 300,
                    "fiber_g": 7,
                    "meals": {
                        "snack": {
                            "items": [
                                {"name": "Орешки со сгущенкой", "kcal": 500},
                                {"name": "Doritos", "kcal": 700},
                            ]
                        }
                    },
                }
            ],
        }

        ideas = compute_food_ideas(data, profile={"avoid_groups": ["legumes"]})
        labels = " ".join(i["title"] for i in ideas).lower()
        raw_sweets = " ".join(i["title"] + " " + i["reason"] for i in ideas).lower()

        self.assertTrue(any(word in labels for word in ["яйца", "курица", "творог", "рыба", "рис", "гречка"]))
        self.assertNotIn("сгущен", raw_sweets)
        self.assertNotIn("doritos", raw_sweets)
        self.assertNotIn("фасоль", labels)
        self.assertNotIn("чечевица", labels)

    def test_food_ideas_include_dish_and_recipe_when_profile_allows(self):
        data = {
            "nutrition_goals": {"protein_g": 120},
            "nutrition": [
                {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "total_kcal": 1600,
                    "protein_g": 55,
                    "fat_g": 65,
                    "carb_g": 190,
                    "fiber_g": 8,
                    "meals": {"lunch": {"items": [{"name": "Куриная отбивная", "kcal": 250}]}}
                }
            ],
        }

        ideas = compute_food_ideas(data, profile={
            "avoid_groups": ["legumes"],
            "recipe_mode": True,
            "max_cook_minutes": 25,
        })

        self.assertTrue(all("dish" in i and i["dish"] for i in ideas))
        self.assertTrue(all("recipe" in i and i["recipe"] for i in ideas))
        self.assertTrue(any("кур" in i["dish"].lower() or "омлет" in i["dish"].lower() for i in ideas))

    def test_deep_food_survey_is_normalized_into_working_profile(self):
        survey = {
            "q1": "больше энергии",
            "q2": ["вкус", "простота", "быстрота"],
            "q9": ["печень", "бобовые", "орехи"],
            "q16": "больше 30 минут, если оно того стоит",
            "q17": ["очень быстро", "долго хранится"],
            "q19": ["всё в одной тарелке", "бутерброды / тосты / лаваш"],
            "q20_куриное филе": "Люблю",
            "q20_печень": "Не ем",
            "q24_омлет": "Люблю",
            "q28_рис белый": "Нормально",
            "q32_помидоры": "Люблю",
            "q36_фасоль": "Не ем",
            "q36_чечевица": "Не ем",
            "q38_семечки": "Не ем",
            "q47_супы": "Не ем",
            "q47_рис с курицей": "Нравится",
            "q49": ["мясо", "яйца", "молочка"],
            "q51": ["когда долго не ел", "когда устал"],
            "q59": "есть аэрогриль, духовка и плита",
        }

        profile = normalize_food_profile({"survey_answers": survey})

        self.assertIn("legumes", profile["avoid_groups"])
        self.assertIn("nuts", profile["avoid_groups"])
        self.assertIn("печень", profile["disliked_ingredients"])
        self.assertIn("фасоль", profile["disliked_ingredients"])
        self.assertIn("куриное филе", profile["preferred_proteins"])
        self.assertIn("омлет", profile["preferred_proteins"])
        self.assertIn("рис белый", profile["preferred_sides"])
        self.assertIn("помидоры", profile["preferred_vegetables"])
        self.assertIn("рис с курицей", profile["preferred_dishes"])
        self.assertIn("аэрогриль", profile["kitchen_equipment"])
        self.assertIn("больше энергии", profile["notes"])

    def test_notes_status_reports_missing_recent_notes(self):
        old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        data = {
            "feelings": [
                {
                    "date": old_date,
                    "yazio_note": None,
                    "manual_note": "старая заметка",
                    "manual_tags": ["pc"],
                    "yazio_tags": [],
                }
            ]
        }

        status = compute_notes_status(data, recent_days=7)

        self.assertEqual(status["total"], 1)
        self.assertEqual(status["manual"], 1)
        self.assertEqual(status["yazio"], 0)
        self.assertFalse(status["has_recent"])
        self.assertIn("нет свежих", status["message"].lower())

    def test_manual_notes_keep_individual_entries_for_deletion(self):
        text, tags, entries = _format_manual_note_entries([
            {"text": "first note", "time": "10:00", "tags": ["sleep"], "added_at": "a"},
            {"text": "second note", "time": "11:00", "tags": ["food"], "added_at": "b"},
        ])

        self.assertEqual(text, "[10:00] first note\n\n[11:00] second note")
        self.assertEqual(tags, ["food", "sleep"])
        self.assertEqual(entries, [
            {"index": 0, "text": "first note", "time": "10:00", "tags": ["sleep"], "added_at": "a"},
            {"index": 1, "text": "second note", "time": "11:00", "tags": ["food"], "added_at": "b"},
        ])

    def test_energy_balance_uses_exercise_without_double_counting_activity(self):
        balance = compute_energy_balance({
            "nutrition_goals": {"kcal": 2000},
            "nutrition": [
                {"date": "2026-05-01", "total_kcal": 2500},
                {"date": "2026-05-02", "total_kcal": 1800},
            ],
            "daily_metrics": [
                {"date": "2026-05-01", "activity": {"calories": 300}},
                {"date": "2026-05-02", "activity": {"calories": 120}},
            ],
            "workouts": [
                {"date": "2026-05-01", "kcal": 500},
                {"date": "2026-05-02", "kcal": 90},
            ],
        }, exercise_credit=0.7, exercise_cap=900)

        first = balance[-2]
        second = balance[-1]
        self.assertEqual(first["exercise_raw"], 500)
        self.assertEqual(first["exercise_credit"], 350)
        self.assertEqual(first["adjusted_goal"], 2350)
        self.assertEqual(first["over_base"], 500)
        self.assertEqual(first["over_adjusted"], 150)
        self.assertEqual(first["remaining"], -150)
        self.assertEqual(second["exercise_raw"], 120)
        self.assertEqual(second["remaining"], 284)

    def test_nutrition_diary_summarizes_today_and_rollups(self):
        data = {
            "nutrition_goals": {"kcal": 2000, "protein_g": 100, "fat_g": 70, "carb_g": 250},
            "nutrition": [
                {
                    "date": "2026-05-02",
                    "total_kcal": 2100,
                    "protein_g": 80,
                    "fat_g": 90,
                    "carb_g": 200,
                    "meals": {"breakfast": {"kcal": 600, "protein_g": 20, "fat_g": 20, "carb_g": 80, "items": []}},
                },
                {"date": "2026-05-01", "total_kcal": 1500, "protein_g": 60, "fat_g": 50, "carb_g": 170, "meals": {}},
            ],
            "daily_metrics": [
                {"date": "2026-05-02", "activity": {"calories": 300}},
                {"date": "2026-05-01", "activity": {"calories": 0}},
            ],
        }

        diary = compute_nutrition_diary(data)

        self.assertEqual(diary["today"]["date"], "2026-05-02")
        self.assertEqual(diary["today"]["remaining"], 110)
        self.assertEqual(diary["today"]["macro_goals"]["protein_g"], 100)
        self.assertEqual(diary["today"]["meal_targets"]["breakfast"], 600)
        self.assertEqual(diary["rollups"]["week"]["remaining"], 610)
        self.assertEqual(len(diary["days"]), 2)

    def test_energy_rollups_keep_week_and_month_totals(self):
        rows = [
            {"date": f"2026-05-{day:02d}", "remaining": 100, "over_adjusted": 0, "base_delta": -50, "exercise_credit": 10}
            for day in range(1, 10)
        ]

        rollups = compute_energy_rollups(rows)

        self.assertEqual(rollups["week"]["days"], 7)
        self.assertEqual(rollups["week"]["remaining"], 700)
        self.assertEqual(rollups["week"]["avg_remaining"], 100)
        self.assertEqual(rollups["week"]["over_days"], 0)
        self.assertEqual(rollups["month"]["days"], 9)
        self.assertEqual(rollups["month"]["exercise_credit"], 90)

    def test_energy_rollups_count_days_with_over_budget(self):
        rows = [
            {"date": "2026-05-01", "remaining": 300, "over_adjusted": 0, "base_delta": -100, "exercise_credit": 100},
            {"date": "2026-05-02", "remaining": -200, "over_adjusted": 200, "base_delta": 250, "exercise_credit": 50},
            {"date": "2026-05-03", "remaining": 50, "over_adjusted": 0, "base_delta": -20, "exercise_credit": 0},
        ]

        rollups = compute_energy_rollups(rows)

        self.assertEqual(rollups["week"]["remaining"], 150)
        self.assertEqual(rollups["week"]["avg_remaining"], 50)
        self.assertEqual(rollups["week"]["over_days"], 1)
        self.assertEqual(rollups["week"]["under_days"], 2)

    def test_sync_notice_warns_when_cached_phone_data_is_used(self):
        notice = compute_sync_notice({
            "sync_status": {"used_cached_dbs": True, "phone_online": False},
            "nutrition": [{"date": "2026-05-02"}],
        }, today=datetime(2026, 5, 4).date())

        self.assertEqual(notice["class"], "warn")
        self.assertIn("телефон недоступен", notice["text"])
        self.assertIn("питание до 2026-05-02", notice["text"])

    def test_sync_notice_is_quiet_for_fresh_phone_data(self):
        notice = compute_sync_notice({
            "sync_status": {"used_cached_dbs": False, "phone_online": True},
            "nutrition": [{"date": "2026-05-03"}],
        }, today=datetime(2026, 5, 4).date())

        self.assertEqual(notice["class"], "")
        self.assertEqual(notice["text"], "")

    def test_yazio_day_summary_replaces_stale_consumed_history_for_same_date(self):
        stale_deleted = {
            "id": "deleted",
            "date": "2026-04-23 05:08:45",
            "daytime": "snack",
            "product_id": "doritos",
        }
        actual = {
            "id": "actual",
            "date": "2026-04-23 05:09:40",
            "daytime": "snack",
            "product_id": "barbecue-doritos",
        }
        next_day = {
            "id": "next-day",
            "date": "2026-04-24 10:00:00",
            "daytime": "breakfast",
            "product_id": "eggs",
        }

        chosen = _choose_yazio_consumed_items(
            [stale_deleted, actual, next_day],
            {"2026-04-23": [actual]},
        )

        self.assertEqual([item["id"] for item in chosen], ["actual", "next-day"])

    def test_yazio_google_fit_trainings_are_parsed_as_workouts(self):
        value = {
            "doneTrainings": [
                {
                    "id": "w1",
                    "training": "Strengthtraining",
                    "dateTime": "2026-04-25 15:20:32",
                    "durationInMinutes": 69,
                    "energyBurned": 383.2814,
                    "distance": 172.0,
                    "steps": 396,
                    "sourceMetaData": {"gateway": "HealthConnect", "source": "GoogleFit"},
                }
            ],
            "stepEntry": {"date": "2026-04-25", "steps": 1861},
        }

        workouts = _parse_yazio_done_trainings("2026-04-25", value)

        self.assertEqual(len(workouts), 1)
        self.assertEqual(workouts[0]["date"], "2026-04-25")
        self.assertEqual(workouts[0]["datetime"], "2026-04-25 15:20:32")
        self.assertEqual(workouts[0]["training"], "Strengthtraining")
        self.assertEqual(workouts[0]["duration_min"], 69)
        self.assertAlmostEqual(workouts[0]["kcal"], 383.3)
        self.assertEqual(workouts[0]["source"], "GoogleFit")

    def test_activity_chart_includes_google_fit_workout_minutes(self):
        charts = format_chart_data({
            "daily_metrics": [
                {"date": "2026-04-25", "activity": {"steps": 1000, "active_min": 10, "calories": 40}},
                {"date": "2026-04-26", "activity": {"steps": 2000, "active_min": 20, "calories": 80}},
            ],
            "workouts": [
                {"date": "2026-04-25", "duration_min": 69, "kcal": 383.3},
                {"date": "2026-04-25", "duration_min": 9, "kcal": 54.6},
            ],
        })

        idx = charts["activity"]["labels"].index("2026-04-25")
        self.assertEqual(charts["activity"]["workout_min"][idx], 78)
        self.assertAlmostEqual(charts["activity"]["workout_kcal"][idx], 437.9)

    def test_ks_fit_walking_sessions_are_labeled_as_treadmill_walks(self):
        training = _healthconnect_training_type(
            app_package="com.kingsmith.xiaojin",
            app_name="KS Fit",
            exercise_type=53,
            title="WALKING",
            client_record_id=None,
        )

        self.assertEqual(training, "walking_treadmill")

    def test_ks_fit_adjacent_fragments_are_merged_into_one_treadmill_workout(self):
        hc_fragments = [
            {
                "id": "hc_1",
                "date": "2026-05-20",
                "datetime": "2026-05-20 01:46:11",
                "training": "walking_treadmill",
                "training_ru": "Ходьба на дорожке",
                "duration_min": 3.4,
                "kcal": 6.0,
                "steps": 166,
                "distance_km": 0.10,
                "source": "HealthConnect",
                "source_app": "KS Fit",
                "source_package": "com.kingsmith.xiaojin",
                "_start_ms": 100_000,
                "_end_ms": 304_000,
            },
            {
                "id": "hc_2",
                "date": "2026-05-20",
                "datetime": "2026-05-20 01:49:35",
                "training": "walking_treadmill",
                "training_ru": "Ходьба на дорожке",
                "duration_min": 0.9,
                "kcal": 1.0,
                "steps": 37,
                "distance_km": 0.03,
                "source": "HealthConnect",
                "source_app": "KS Fit",
                "source_package": "com.kingsmith.xiaojin",
                "_start_ms": 304_000,
                "_end_ms": 358_000,
            },
            {
                "id": "hc_3",
                "date": "2026-05-20",
                "datetime": "2026-05-20 01:50:30",
                "training": "walking_treadmill",
                "training_ru": "Ходьба на дорожке",
                "duration_min": 0.5,
                "kcal": 1.0,
                "steps": 22,
                "distance_km": 0.02,
                "source": "HealthConnect",
                "source_app": "KS Fit",
                "source_package": "com.kingsmith.xiaojin",
                "_start_ms": 358_000,
                "_end_ms": 388_000,
            },
        ]

        workouts = _merge_workouts([], hc_fragments)

        self.assertEqual(len(workouts), 1)
        workout = workouts[0]
        self.assertEqual(workout["training"], "walking_treadmill")
        self.assertEqual(workout["training_ru"], "Ходьба на дорожке")
        self.assertEqual(workout["source_app"], "KS Fit")
        self.assertEqual(workout["fragment_count"], 3)
        self.assertAlmostEqual(workout["duration_min"], 4.8)
        self.assertAlmostEqual(workout["kcal"], 8.0)
        self.assertEqual(workout["steps"], 225)
        self.assertAlmostEqual(workout["distance_km"], 0.15)
        self.assertAlmostEqual(workout["avg_speed_kmh"], 1.9)

    def test_yazio_feelings_archive_keeps_old_seen_notes(self):
        archived = {
            "2026-04-20": {"date": "2026-04-20", "note": "старая заметка", "tags": ["Cold"]}
        }
        current = [
            {"date": "2026-04-26", "note": "новая заметка", "tags": ["Fatigue"]},
            {"date": "2026-04-27", "note": None, "tags": []},
        ]

        merged = _merge_yazio_feelings_archive(current, archived)

        self.assertIn("2026-04-20", merged)
        self.assertIn("2026-04-26", merged)
        self.assertNotIn("2026-04-27", merged)
        self.assertEqual(merged["2026-04-20"]["note"], "старая заметка")

    def test_section_kpis_are_contextual_and_have_four_cards_each(self):
        data = {
            "nutrition_goals": {"weight_goal_kg": 80, "protein_g": 150},
            "daily_metrics": [
                {
                    "date": "2026-06-06",
                    "activity": {"steps": 2000, "active_min": 40, "distance_m": 2000},
                    "hr": {"avg": 70},
                    "stress": {"avg": 35},
                    "spo2": {"avg": 97},
                },
                {
                    "date": "2026-06-05",
                    "activity": {"steps": 1000, "active_min": 20, "distance_m": 1000},
                    "hr": {"avg": 66},
                    "stress": {"avg": 25},
                    "spo2": {"avg": 98},
                },
            ],
            "nutrition": [
                {"date": "2026-06-06", "total_kcal": 1800, "protein_g": 90, "fat_g": 70, "carb_g": 200},
            ],
            "workouts": [{"date": "2026-06-06"}],
            "sleep_sessions": [],
        }
        kpis = compute_section_kpis(
            data,
            insights={"current_weight": 97.2, "body_fat": 31.2, "muscle": 63.5},
            sleep_metrics={"recent_nights_count": 0},
            food_profile={"survey_answers": {"q1": "yes"}, "preferred_proteins": ["рыба"]},
            notes_status={"total": 3, "recent": 1, "yazio": 2, "manual": 1},
        )

        self.assertEqual(
            set(kpis),
            {"sleep", "body", "nutrition", "foodprofile", "activity", "health", "notes"},
        )
        self.assertTrue(all(len(cards) == 4 for cards in kpis.values()))
        self.assertEqual(kpis["activity"][0]["value"], 1500)
        self.assertEqual(kpis["nutrition"][1]["label"], "Белок")
        self.assertEqual(kpis["notes"][0]["value"], 3)


if __name__ == "__main__":
    unittest.main()
