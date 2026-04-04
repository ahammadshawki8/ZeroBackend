"""Validation and fallback mixin for AI service."""

import hashlib
from typing import Any, Dict, List


class AIServiceValidationMixin:

    def _normalize_confidence(self, raw_value: Any, default: int = 80) -> int:
        """Normalize confidence from either 0-1 or 0-100 formats into 0-100 int."""
        try:
            if raw_value is None:
                return default

            if isinstance(raw_value, str):
                cleaned = raw_value.strip().rstrip('%')
                if cleaned == '':
                    return default
                value = float(cleaned)
            else:
                value = float(raw_value)

            # Many model responses return confidence as 0-1 decimal.
            if 0 <= value <= 1:
                value *= 100

            return max(0, min(100, int(round(value))))
        except Exception:
            return default

    def _validate_analysis_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        defaults = {
            "description": "Waste materials detected",
            "severity": "MEDIUM",
            "estimatedVolume": "Moderate amount",
            "environmentalImpact": "MODERATE",
            "healthHazard": False,
            "hazardDetails": "",
            "recommendedAction": "Cleanup recommended",
            "estimatedCleanupTime": "1-2 hours",
            "confidence": 80,
            "wasteComposition": [{"type": "Unknown/Other", "percentage": 100, "recyclable": False}],
            "specialEquipmentNeeded": ["Gloves", "Waste bags"],
        }

        for key, default_value in defaults.items():
            if key not in data:
                data[key] = default_value

        if data["severity"] not in ["LOW", "MEDIUM", "HIGH", "CRITICAL"]:
            data["severity"] = "MEDIUM"

        if data["environmentalImpact"] not in ["LOW", "MODERATE", "HIGH", "SEVERE"]:
            data["environmentalImpact"] = "MODERATE"

        data["confidence"] = self._normalize_confidence(data.get("confidence", 80), default=80)

        # Normalize composition percentages.
        composition = data.get("wasteComposition") or []
        if not isinstance(composition, list) or len(composition) == 0:
            composition = [{"type": "Unknown/Other", "percentage": 100, "recyclable": False}]

        cleaned: List[Dict[str, Any]] = []
        total = 0
        for item in composition:
            waste_type = str(item.get("type", "Unknown/Other"))
            percentage = int(item.get("percentage", 0))
            percentage = max(0, min(100, percentage))
            recyclable = bool(item.get("recyclable", False))
            cleaned.append({"type": waste_type, "percentage": percentage, "recyclable": recyclable})
            total += percentage

        if total == 0:
            cleaned = [{"type": "Unknown/Other", "percentage": 100, "recyclable": False}]
        elif total != 100:
            # Scale percentages to sum to 100 while preserving relative proportions.
            scaled: List[Dict[str, Any]] = []
            running = 0
            for idx, item in enumerate(cleaned):
                if idx == len(cleaned) - 1:
                    pct = max(0, 100 - running)
                else:
                    pct = round((item["percentage"] / total) * 100)
                    running += pct
                scaled.append({**item, "percentage": pct})
            cleaned = scaled

        data["wasteComposition"] = cleaned

        if not isinstance(data.get("specialEquipmentNeeded"), list):
            data["specialEquipmentNeeded"] = ["Gloves", "Waste bags"]

        return data

    def _validate_comparison_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        defaults = {
            "completionPercentage": 80,
            "beforeSummary": "Area before cleanup",
            "afterSummary": "Area after cleanup",
            "qualityRating": "GOOD",
            "environmentalBenefit": "Improved cleanliness",
            "verificationStatus": "VERIFIED",
            "feedback": "Cleanup completed",
            "confidence": 80,
            "wasteRemoved": [{"type": "Mixed", "percentage": 100, "recyclable": False}],
            "remainingIssues": [],
        }

        for key, default_value in defaults.items():
            if key not in data:
                data[key] = default_value

        if data["qualityRating"] not in ["POOR", "FAIR", "GOOD", "EXCELLENT"]:
            data["qualityRating"] = "GOOD"

        if data["verificationStatus"] not in ["VERIFIED", "NEEDS_REVIEW", "INCOMPLETE"]:
            data["verificationStatus"] = "VERIFIED"

        data["completionPercentage"] = max(0, min(100, int(data.get("completionPercentage", 80))))
        data["confidence"] = self._normalize_confidence(data.get("confidence", 80), default=80)

        if not isinstance(data.get("wasteRemoved"), list) or not data["wasteRemoved"]:
            data["wasteRemoved"] = [{"type": "Mixed", "percentage": 100, "recyclable": False}]

        if not isinstance(data.get("remainingIssues"), list):
            data["remainingIssues"] = []

        return data

    def _get_contextual_fallback_analysis(self, image_input: str) -> Dict[str, Any]:
        """
        Conservative fallback that avoids overconfident misclassification.
        """
        seed = int(hashlib.md5(image_input.encode("utf-8", errors="ignore")).hexdigest()[:8], 16)
        confidence = 35 + (seed % 10)

        return {
            "description": "Automatic visual extraction failed or was inconclusive. A conservative fallback analysis was returned.",
            "severity": "LOW",
            "estimatedVolume": "Unable to estimate reliably from image",
            "environmentalImpact": "LOW",
            "healthHazard": False,
            "hazardDetails": "",
            "recommendedAction": "Request a clearer, close-range image with good lighting for accurate material classification.",
            "estimatedCleanupTime": "Manual review required",
            "confidence": confidence,
            "wasteComposition": [
                {"type": "Unknown/Other", "percentage": 100, "recyclable": False}
            ],
            "specialEquipmentNeeded": ["Manual inspection", "Basic PPE"],
        }

    def _get_contextual_fallback_comparison(self, before_url: str, after_url: str) -> Dict[str, Any]:
        seed = int(hashlib.md5((before_url + after_url).encode("utf-8", errors="ignore")).hexdigest()[:8], 16)
        completion = 60 + (seed % 20)

        return {
            "completionPercentage": completion,
            "beforeSummary": "Could not reliably parse before image details.",
            "afterSummary": "Could not reliably parse after image details.",
            "qualityRating": "POOR" if completion < 70 else "FAIR",
            "environmentalBenefit": "Manual verification required due to low image-analysis confidence.",
            "verificationStatus": "NEEDS_REVIEW",
            "feedback": "Upload clearer before/after images for robust AI verification.",
            "confidence": 45,
            "wasteRemoved": [{"type": "Unknown/Other", "percentage": 100, "recyclable": False}],
            "remainingIssues": ["Manual verification required"],
        }


