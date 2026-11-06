import tempfile
import unittest
from pathlib import Path
from autopapercrawl.registry import Registry
from autopapercrawl.venues import venue_matches
from autopapercrawl.adapters.dblp import parse_dblp_key


class RegistryTests(unittest.TestCase):
    def test_required_venues_and_domain_templates(self):
        registry = Registry()
        venues = registry.list("venues")
        self.assertGreaterEqual(len(venues), 56)
        for identifier in ("sp", "ccs", "jmlr", "tpami", "tnnls", "tacl", "vldb", "tods", "sosp", "ase"):
            self.assertIn(identifier, venues)
            self.assertEqual(venues[identifier]["capability_status"], "configured")
        self.assertIn("robotics", registry.topics())
        self.assertTrue(venue_matches("2025 ACM SIGSAC Conference on Computer and Communications Security", "ccs"))
        self.assertFalse(venue_matches("Space science", "sp"))

    def test_custom_crud_persists_without_modifying_builtins(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(Path(directory))
            registry.save("domains", {"domain_id": "biology", "domain_name": "Biology", "keywords": ["genomics"]})
            self.assertIn("biology", Registry(Path(directory)).topics())
            registry.save("domains", {"domain_id": "biology", "domain_name": "Biology", "keywords": ["proteomics"]})
            self.assertEqual(registry.topics()["biology"]["queries"], ["proteomics"])
            registry.delete("domains", "biology")
            self.assertNotIn("biology", registry.topics())
            self.assertIn("ai_security", registry.topics())
            with self.assertRaises(ValueError):
                registry.save("domains", {"domain_id": "../escape", "domain_name": "x", "keywords": ["x"]})
            with self.assertRaises(ValueError):
                registry.save("venues", {"venue_id": "new", "name": "New", "venue_type": "journal", "ranking_tags": [{"rank": "A"}]})

    def test_dblp_url_formats(self):
        for value in ("conf/sp", "https://dblp.org/db/conf/sp/index.html", "https://dblp.org/db/conf/sp/sp2024.html", "https://dblp.org/streams/conf/sp.xml"):
            self.assertEqual(parse_dblp_key(value), "conf/sp")
        with self.assertRaises(ValueError):
            parse_dblp_key("https://unrelated.example/conf/sp")

    def test_ids_are_generated_and_remain_stable_when_editing(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(Path(directory))
            for kind, fields, id_field in [
                ("domains", {"domain_name": "安全分析", "keywords": ["threat analysis"]}, "domain_id"),
                ("venues", {"name": "安全会议", "venue_type": "conference"}, "venue_id"),
            ]:
                record = registry.save(kind, fields)
                identifier = record[id_field]
                self.assertRegex(identifier, r"^custom_[a-f0-9]{32}$")
                self.assertEqual(registry.save(kind, {**record, "description": "Updated"})[id_field], identifier)
                self.assertIn(identifier, Registry(Path(directory)).list(kind))
                self.assertNotEqual(registry.save(kind, fields)[id_field], identifier)

    def test_shortcuts_default_to_four_and_can_be_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(Path(directory))
            self.assertEqual({key for key, item in registry.topics().items() if item["show_in_shortcuts"]},
                             {"satellite_security", "vulnerability_detection", "encrypted_traffic", "ai_security"})
            record = {"domain_id": "threat_analysis", "domain_name": "Threat analysis", "keywords": ["threat intelligence"], "show_in_shortcuts": False}
            registry.save("domains", record)
            self.assertFalse(Registry(Path(directory)).topics()["threat_analysis"]["show_in_shortcuts"])
            registry.save("domains", {**record, "show_in_shortcuts": True})
            self.assertTrue(Registry(Path(directory)).topics()["threat_analysis"]["show_in_shortcuts"])
            with self.assertRaises(ValueError):
                registry.save("domains", {**record, "show_in_shortcuts": "false"})
