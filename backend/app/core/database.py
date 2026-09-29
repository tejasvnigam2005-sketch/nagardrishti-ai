import uuid
import logging
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from app.core.config import settings
from app.services.duplicate_detector import DuplicateDetector, haversine_distance_meters

logger = logging.getLogger(__name__)

# Initial default departments as seeded in 001_initial_schema.sql
DEFAULT_DEPARTMENTS = [
    {"id": "dept-1", "name": "Municipal Roads", "category": "pothole", "is_active": True},
    {"id": "dept-2", "name": "Sanitation", "category": "garbage", "is_active": True},
    {"id": "dept-3", "name": "Electrical / Municipal Lighting", "category": "streetlight", "is_active": True},
    {"id": "dept-4", "name": "Drainage / Sanitation", "category": "drain", "is_active": True},
    {"id": "dept-5", "name": "Manual Review", "category": "other", "is_active": True},
]


class Database:
    """
    Supabase PostgreSQL client with resilient local fallback and spatial intelligence.
    Maintains 100% API compatibility whether remote Supabase keys are provided or during local testing.
    Production starts with an empty complaints table.
    """

    def __init__(self):
        self._supabase = None
        self._memory_complaints: List[Dict] = []
        self._memory_departments: List[Dict] = [dict(d) for d in DEFAULT_DEPARTMENTS]
        self._report_seq = 1
        self._load_from_disk()
    def _get_storage_paths(self) -> List[Path]:
        paths = []
        p1 = Path(__file__).resolve().parent.parent.parent / "data" / "complaints_db.json"
        try:
            p1.parent.mkdir(parents=True, exist_ok=True)
            paths.append(p1)
        except Exception:
            pass
        # Serverless fallback for writable temp storage
        p2 = Path("/tmp/complaints_db.json")
        paths.append(p2)
        return paths

    def _load_from_disk(self):
        for p in self._get_storage_paths():
            if p.exists() and p.stat().st_size > 5:
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        self._memory_complaints = data
                        logger.info(f"Loaded {len(data)} complaints from shared disk: {p}")
                        max_seq = 1
                        for c in data:
                            rep = str(c.get("report_id", ""))
                            if rep.startswith("NGD-2026-"):
                                try:
                                    s = int(rep.split("-")[-1])
                                    if s > max_seq:
                                        max_seq = s
                                except ValueError:
                                    pass
                        self._report_seq = max_seq + 1
                        return
                except Exception as e:
                    logger.warning(f"Failed to read complaints from disk {p}: {e}")

    def _save_to_disk(self):
        for p in self._get_storage_paths():
            try:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(self._memory_complaints, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Failed to save complaints to disk {p}: {e}")


    def _get_client(self):
        if self._supabase is None and settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
            try:
                from supabase import create_client
                self._supabase = create_client(
                    settings.SUPABASE_URL,
                    settings.SUPABASE_SERVICE_ROLE_KEY
                )
                logger.info("Connected to remote Supabase PostgreSQL database.")
            except Exception as e:
                logger.warning(f"Could not connect to Supabase: {e}. Using fallback datastore.")
        return self._supabase

    async def get_departments(self) -> List[Dict]:
        client = self._get_client()
        if client:
            try:
                res = client.table("departments").select("*").eq("is_active", True).execute()
                if res.data:
                    return res.data
            except Exception as e:
                logger.warning(f"Failed to query departments from Supabase ({e}). Using local departments.")
        return [d for d in self._memory_departments if d.get("is_active", True)]

    async def create_complaint(self, data: dict, citizen_id: Optional[str] = None) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        
        client = self._get_client()
        if client:
            try:
                latest = client.table("complaints").select("report_id").order("created_at", desc=True).limit(30).execute()
                for item in (latest.data or []):
                    rid = item.get("report_id", "")
                    if rid.startswith("NGD-2026-"):
                        try:
                            seq = int(rid.split("-")[-1])
                            if seq >= self._report_seq:
                                self._report_seq = seq + 1
                        except ValueError:
                            pass
            except Exception:
                pass

        report_id = f"NGD-2026-{self._report_seq:05d}"
        self._report_seq += 1

        # Check duplicate against existing complaints
        all_existing = await self.get_complaints()
        duplicate_report_id = DuplicateDetector.check_duplicate(
            new_category=data.get("problem_type"),
            new_lat=data.get("latitude"),
            new_lng=data.get("longitude"),
            existing_reports=all_existing
        )

        complaint_record = {
            "id": str(uuid.uuid4()),
            "report_id": report_id,
            "citizen_id": citizen_id,
            "problem_type": data.get("problem_type"),
            "confidence": float(data.get("confidence", 0.8)),
            "severity": data.get("severity", "LOW"),
            "evidence": data.get("evidence", []),
            "latitude": float(data.get("latitude")),
            "longitude": float(data.get("longitude")),
            "location_name": data.get("location_name", "Reported Location"),
            "department": data.get("department", "Manual Review"),
            "description": data.get("description", ""),
            "image_url": data.get("image_url", ""),
            "status": "REPORTED",
            "duplicate_of": duplicate_report_id or data.get("duplicate_of"),
            "created_at": now,
            "updated_at": now
        }

        # Maintain in-memory store for cache, ownership tracking, and test isolation
        self._memory_complaints.insert(0, dict(complaint_record))
        self._save_to_disk()

        # Automatic sync to Authority Portal (both Render production and localhost)
        try:
            import urllib.request
            auth_payload = json.dumps(complaint_record).encode('utf-8')
            auth_targets = [
                os.getenv('AUTHORITY_API_URL', 'https://nagardrishti-auth.onrender.com'),
                'http://localhost:8000'
            ]
            for target_base in auth_targets:
                if target_base:
                    try:
                        req = urllib.request.Request(
                            f"{target_base.rstrip('/')}/api/complaints",
                            data=auth_payload,
                            headers={'Content-Type': 'application/json'},
                            method='POST'
                        )
                        urllib.request.urlopen(req, timeout=2.5)
                        logger.info(f'Complaint {report_id} automatically synced to authority portal at {target_base}')
                        break
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f'Authority auto-sync skipped: {e}')

        # Try Supabase insert
        client = self._get_client()
        if client:
            try:
                res = client.table("complaints").insert(complaint_record).execute()
                if res.data:
                    logger.info(f"Complaint {report_id} persisted to Supabase.")
                    return res.data[0]
            except Exception as e:
                # If remote table does not yet have citizen_id column, persist remaining fields
                if "citizen_id" in str(e):
                    try:
                        record_compat = {k: v for k, v in complaint_record.items() if k != "citizen_id"}
                        res = client.table("complaints").insert(record_compat).execute()
                        if res.data:
                            logger.info(f"Complaint {report_id} persisted to Supabase (compat mode).")
                            merged = dict(res.data[0])
                            merged["citizen_id"] = citizen_id
                            return merged
                    except Exception as inner_e:
                        logger.warning(f"Supabase fallback insert failed ({inner_e}).")
                logger.warning(f"Failed to insert into Supabase ({e}). Persisting in local storage.")

        return complaint_record

    async def get_complaints(
        self,
        citizen_id: Optional[str] = None,
        problem_type: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        department: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict]:
        client = self._get_client()
        remote_data: List[Dict] = []
        if client:
            try:
                query = client.table("complaints").select("*").order("created_at", desc=True)
                if problem_type:
                    query = query.eq("problem_type", problem_type)
                if severity:
                    query = query.eq("severity", severity)
                if status:
                    query = query.eq("status", status)
                if department:
                    query = query.eq("department", department)
                query = query.limit(limit)
                res = query.execute()
                if res.data is not None:
                    remote_data = res.data
            except Exception as e:
                logger.warning(f"Supabase complaints fetch error ({e}). Using local store.")

        # Combine remote complaints with in-memory complaints, preserving ownership
        combined_map: Dict[str, Dict] = {}
        for c in remote_data:
            key = c.get("report_id") or c.get("id")
            if key:
                combined_map[key] = dict(c)
        for c in self._memory_complaints:
            key = c.get("report_id") or c.get("id")
            if key:
                if key in combined_map:
                    if not combined_map[key].get("citizen_id") and c.get("citizen_id"):
                        combined_map[key]["citizen_id"] = c.get("citizen_id")
                else:
                    combined_map[key] = dict(c)

        filtered = list(combined_map.values())
        filtered.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)

        if citizen_id:
            filtered = [c for c in filtered if c.get("citizen_id") == citizen_id]
        if problem_type:
            filtered = [c for c in filtered if c.get("problem_type") == problem_type]
        if severity:
            filtered = [c for c in filtered if c.get("severity") == severity]
        if status:
            filtered = [c for c in filtered if c.get("status") == status]
        if department:
            filtered = [c for c in filtered if c.get("department") == department]

        return filtered[:limit]

    def _is_uuid(self, val: str) -> bool:
        try:
            uuid.UUID(str(val))
            return True
        except (ValueError, TypeError, AttributeError):
            return False

    async def get_complaint_by_id(
        self,
        id_or_report_id: str,
        citizen_id: Optional[str] = None,
        is_authority: bool = False
    ) -> Optional[Dict]:
        found: Optional[Dict] = None

        # Check local memory first for instant session consistency
        for c in self._memory_complaints:
            if c.get("id") == id_or_report_id or c.get("report_id") == id_or_report_id:
                found = dict(c)
                break

        if not found:
            client = self._get_client()
            if client:
                try:
                    if self._is_uuid(id_or_report_id):
                        res = client.table("complaints").select("*").eq("id", id_or_report_id).execute()
                    else:
                        res = client.table("complaints").select("*").eq("report_id", id_or_report_id).execute()
                    if res.data:
                        found = dict(res.data[0])
                except Exception as e:
                    logger.warning(f"Supabase fetch by ID error: {e}")

        if not found:
            return None

        # Enforce citizen ownership if accessed by citizen
        if not is_authority and citizen_id:
            if found.get("citizen_id") != citizen_id:
                return None

        return found

    async def update_complaint_status(self, id_or_report_id: str, new_status: str) -> Optional[Dict]:
        """
        Updates complaint lifecycle status with authority validation.
        Valid statuses: REPORTED, ASSIGNED, IN_PROGRESS, RESOLVED.
        """
        valid_statuses = {"REPORTED", "ASSIGNED", "IN_PROGRESS", "RESOLVED"}
        if new_status not in valid_statuses:
            raise ValueError(f"Invalid status '{new_status}'. Allowed: {valid_statuses}")

        now = datetime.now(timezone.utc).isoformat()
        client = self._get_client()
        if client:
            try:
                table = client.table("complaints")
                update_data = {
                    "status": new_status,
                    "updated_at": now
                }
                if self._is_uuid(id_or_report_id):
                    res = table.update(update_data).eq("id", id_or_report_id).execute()
                else:
                    res = table.update(update_data).eq("report_id", id_or_report_id).execute()
                if res.data:
                    return res.data[0]
            except Exception as e:
                logger.warning(f"Supabase update error: {e}")

        for c in self._memory_complaints:
            if c.get("id") == id_or_report_id or c.get("report_id") == id_or_report_id:
                c["status"] = new_status
                c["updated_at"] = now
                self._save_to_disk()
                return c
        return None

    def calculate_hotspots(self, complaints: List[Dict]) -> List[Dict]:
        """
        Deterministic spatial clustering algorithm for Hotspot Intelligence.
        Groups complaint coordinates into geographic clusters (approx 1.2km radius),
        calculating centroids, unresolved ratios, dominant problem types, and trends.
        """
        if not complaints:
            return []

        clusters: List[Dict] = []
        MAX_CLUSTER_DIST_METERS = 1400.0  # ~1.4 km corridor radius

        for c in complaints:
            lat = c.get("latitude")
            lng = c.get("longitude")
            if lat is None or lng is None:
                continue

            matched_cluster = None
            for cl in clusters:
                dist = haversine_distance_meters(lat, lng, cl["centroid_lat"], cl["centroid_lng"])
                if dist <= MAX_CLUSTER_DIST_METERS:
                    matched_cluster = cl
                    break

            if matched_cluster:
                matched_cluster["reports"].append(c)
                # Recompute centroid
                n = len(matched_cluster["reports"])
                matched_cluster["centroid_lat"] = sum(r["latitude"] for r in matched_cluster["reports"]) / n
                matched_cluster["centroid_lng"] = sum(r["longitude"] for r in matched_cluster["reports"]) / n
                # Track max radius
                d = haversine_distance_meters(lat, lng, matched_cluster["centroid_lat"], matched_cluster["centroid_lng"])
                if d > matched_cluster["max_dist_m"]:
                    matched_cluster["max_dist_m"] = d
            else:
                clusters.append({
                    "centroid_lat": lat,
                    "centroid_lng": lng,
                    "max_dist_m": 400.0,
                    "reports": [c]
                })

        hotspot_list = []
        now_3d = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        for idx, cl in enumerate(clusters):
            reps = cl["reports"]
            total_reps = len(reps)

            cat_counts: Dict[str, int] = {}
            for r in reps:
                cat = r.get("problem_type", "other")
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

            dominant_cat = max(cat_counts, key=cat_counts.get) if cat_counts else "other"
            repeated_count = cat_counts.get(dominant_cat, 0)
            unresolved = sum(1 for r in reps if r.get("status") != "RESOLVED")
            high_critical = sum(1 for r in reps if r.get("severity") in ["HIGH", "CRITICAL"])

            title_map = {
                "pothole": "ROAD SAFETY HOTSPOT",
                "garbage": "SOLID WASTE ACCUMULATION HOTSPOT",
                "streetlight": "LIGHTING & ELECTRICAL HAZARD HOTSPOT",
                "drain": "DRAINAGE & STORM OVERFLOW HOTSPOT",
                "other": "CIVIC INFRASTRUCTURE HOTSPOT"
            }
            action_map = {
                "pothole": "Inspect affected road corridor & schedule asphalt patching crew",
                "garbage": "Deploy municipal sanitation vehicle & clear pedestrian blockage",
                "streetlight": "Dispatch electrical inspection team & secure overhead wiring",
                "drain": "Clear inlet obstruction & deploy stormwater drainage pumps",
                "other": "Conduct on-site civic inspection with local zonal officer"
            }

            title = title_map.get(dominant_cat, "MUNICIPAL CIVIC HOTSPOT")
            action = action_map.get(dominant_cat, "Inspect affected municipal zone")
            radius_km = round(max(0.4, cl["max_dist_m"] / 1000.0), 1)

            recent_count = sum(1 for r in reps if r.get("created_at") and r["created_at"] >= now_3d)
            trend_pct = int((recent_count / total_reps) * 45) if total_reps > 0 else 10

            hotspot_list.append({
                "id": f"hs-{idx + 1}",
                "title": title,
                "dominant_issue": f"{dominant_cat.capitalize()} ({repeated_count} reports in corridor)",
                "total_reports": total_reps,
                "unresolved_count": unresolved,
                "high_critical_count": high_critical,
                "trend_percentage": max(12, trend_pct),
                "suggested_action": action,
                "latitude": round(cl["centroid_lat"], 5),
                "longitude": round(cl["centroid_lng"], 5),
                "radius_km": radius_km,
                "repeated_count": repeated_count,
                "report_ids": [r.get("report_id") for r in reps]
            })

        hotspot_list.sort(key=lambda h: h["total_reports"], reverse=True)
        return hotspot_list

    def calculate_daily_trends(self, complaints: List[Dict]) -> List[Dict]:
        """
        Computes actual 7-day complaint intake volume from database records.
        Returns ordered list of daily trend points (Mon-Sun).
        """
        day_counts: Dict[str, int] = {}
        day_labels: Dict[str, str] = {}
        now = datetime.now(timezone.utc)

        for i in range(6, -1, -1):
            dt = now - timedelta(days=i)
            key = dt.strftime("%Y-%m-%d")
            day_counts[key] = 0
            day_labels[key] = dt.strftime("%a")

        for c in complaints:
            ca = c.get("created_at")
            if ca:
                try:
                    dt = datetime.fromisoformat(str(ca).replace("Z", "+00:00"))
                    key = dt.strftime("%Y-%m-%d")
                    if key in day_counts:
                        day_counts[key] += 1
                except Exception:
                    pass

        trends = []
        for key in sorted(day_counts.keys()):
            trends.append({
                "date": key,
                "day_label": day_labels.get(key, key),
                "count": day_counts[key]
            })
        return trends

    def calculate_statistics(self, complaints: List[Dict]) -> Dict:
        """
        Computes dashboard statistics, category distributions,
        7-day trends, and spatial hotspots from given complaints list.
        Handles zero-report empty state gracefully.
        """
        total = len(complaints)
        high_critical = sum(1 for c in complaints if c.get("severity") in ["HIGH", "CRITICAL"])
        pending = sum(1 for c in complaints if c.get("status") == "REPORTED")
        in_progress = sum(1 for c in complaints if c.get("status") in ["ASSIGNED", "IN_PROGRESS"])
        resolved = sum(1 for c in complaints if c.get("status") == "RESOLVED")

        by_category = {"pothole": 0, "garbage": 0, "streetlight": 0, "drain": 0, "other": 0}
        for c in complaints:
            cat = c.get("problem_type", "other")
            by_category[cat] = by_category.get(cat, 0) + 1

        by_severity = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
        for c in complaints:
            sev = c.get("severity", "LOW")
            by_severity[sev] = by_severity.get(sev, 0) + 1

        by_status = {"REPORTED": 0, "ASSIGNED": 0, "IN_PROGRESS": 0, "RESOLVED": 0}
        for c in complaints:
            st = c.get("status", "REPORTED")
            by_status[st] = by_status.get(st, 0) + 1

        hotspots = self.calculate_hotspots(complaints)
        daily_trends = self.calculate_daily_trends(complaints)

        return {
            "total_reports": total,
            "high_critical": high_critical,
            "pending": pending,
            "in_progress": in_progress,
            "resolved": resolved,
            "by_category": by_category,
            "by_severity": by_severity,
            "by_status": by_status,
            "hotspots": hotspots,
            "daily_trends": daily_trends
        }

    async def get_dashboard_stats(self) -> Dict:
        complaints = await self.get_complaints(limit=500)
        return self.calculate_statistics(complaints)

    async def get_heatmap_points(self) -> List[Dict]:
        complaints = await self.get_complaints(limit=500)
        severity_weights = {
            "LOW": 0.35,
            "MEDIUM": 0.60,
            "HIGH": 0.85,
            "CRITICAL": 1.00
        }

        points = []
        for c in complaints:
            if c.get("latitude") and c.get("longitude"):
                points.append({
                    "latitude": c["latitude"],
                    "longitude": c["longitude"],
                    "weight": severity_weights.get(c.get("severity", "LOW"), 0.5),
                    "problem_type": c.get("problem_type", "other"),
                    "severity": c.get("severity", "LOW"),
                    "report_id": c.get("report_id", "")
                })
        return points


db = Database()

def calculate_statistics(complaints: List[Dict]) -> Dict:
    return db.calculate_statistics(complaints)

def calculate_hotspots(complaints: List[Dict]) -> List[Dict]:
    return db.calculate_hotspots(complaints)

def calculate_daily_trends(complaints: List[Dict]) -> List[Dict]:
    return db.calculate_daily_trends(complaints)
