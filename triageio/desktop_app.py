import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import seaborn as sns
import numpy as np
from matplotlib.ticker import StrMethodFormatter
import json
import os
import re
from datetime import datetime
from triageio.processor import TelemetryProcessor
from triageio.analyzer import TelemetryAnalyzer
from triageio.sanitize import sanitize_df

# --- MASTER DESIGN PALETTE ---
BG_MAIN, BG_CARD = "#0f172a", "#1e293b"
FG_TEXT, FG_SUB  = "#f8fafc", "#94a3b8"
ACCENT, SUCCESS, WARNING, ERROR = "#3b82f6", "#10b981", "#f59e0b", "#ef4444"
SEC_COLORS = {
    "FOR EVERYONE":   "#60a5fa",
    "HOW TO READ":    "#34d399",
    "KEY THRESHOLDS": "#f59e0b",
    "EXPERT NOTES":   "#a78bfa",
    "ACTION GUIDE":   "#fb7185",
}

# ======================================================================
#  GUI UTILITIES
# ======================================================================
class SelectableLabel(tk.Label):
    """A Label that automatically wraps its text and supports right-click copying."""
    def __init__(self, master, text, **kwargs):
        # Default styling to match previous Text-based label
        style = {
            'bg': BG_CARD, 'fg': FG_TEXT, 'font': ("Segoe UI", 10),
            'justify': 'left', 'anchor': 'w',
        }
        style.update(kwargs)
        super().__init__(master, text=text, **style)
        
        # Standard right-click menu for copying
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Copy Text", command=self._copy_to_clipboard)
        self.bind("<Button-3>", lambda e: self.menu.post(e.x_root, e.y_root))
        
        # Auto-wrap logic
        self.bind("<Configure>", self._on_configure)

    def _copy_to_clipboard(self):
        self.clipboard_clear()
        self.clipboard_append(self.cget("text"))

    def _on_configure(self, event):
        """Update wraplength to match the widget's actual width."""
        # Subtract a small buffer to prevent triggering scrollbars in parent frames
        self.configure(wraplength=event.width - 4)

def apply_smart_limits(ax, data, metric):
    """Ensures graph Y-axis covers both data range and critical thresholds."""
    if data is None or data.empty:
        return
    
    y_min, y_max = data.min(), data.max()
    
    # Check enterprise thresholds for this metric
    cfg = ENTERPRISE_THRESHOLDS.get(metric)
    if cfg:
        # Include line values in the range
        for val, _, _, _ in cfg.get("lines", []):
            y_min = min(y_min, val)
            y_max = max(y_max, val)
        # Include span lower bounds only — upper bounds can be open-ended sentinels (e.g. 99999)
        # and would blow out the axis scale. Lines already cover the meaningful markers.
        for lo, _hi, _, _, _ in cfg.get("spans", []):
            y_min = min(y_min, lo)

    # Add 10% padding
    pad = (y_max - y_min) * 0.1 if y_max != y_min else 1.0
    ax.set_ylim(y_min - pad, y_max + pad)

def add_entry_menu(entry):
    """Adds a standard right-click menu to an Entry widget."""
    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Cut", command=lambda: entry.event_generate("<<Cut>>"))
    menu.add_command(label="Copy", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Paste", command=lambda: entry.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="Select All", command=lambda: (entry.focus_set(), entry.selection_range(0, 'end')))
    entry.bind("<Button-3>", lambda e: menu.post(e.x_root, e.y_root))

# ======================================================================
#  ENTERPRISE PASS/FAIL THRESHOLDS
#  Sources: JEDEC JESD218B, JESD219A, NVMe Spec 1.4c, ATA/SATA IO Spec,
#           OCP Datacenter NVMe SSD Spec v2.5, vendor application notes
# ======================================================================
ENTERPRISE_THRESHOLDS = {
    "Temperature": {
        # (y_lo, y_hi, color, alpha, label)
        "spans": [(0, 40, SUCCESS, 0.07, "Optimal"), (40, 55, SUCCESS, 0.04, "Normal"),
                  (55, 70, WARNING, 0.10, "Caution"), (70, 130, ERROR, 0.18, "FAIL")],
        # (y_val, color, linestyle, label)
        "lines": [(55, WARNING, ":", "55\u00b0C Caution"),
                  (70, ERROR,   "--", "70\u00b0C JEDEC Limit  |  JESD219A")],
        "source": "JEDEC JESD219A Table 3 \u2014 Enterprise Class A",
    },
    "Available Spare": {
        "spans": [(90, 100, SUCCESS, 0.07, "PASS (\u226590%)"),
                  (10,  90, WARNING, 0.09, "Caution (10\u201390%)"),
                  (0,   10, ERROR,   0.20, "JEDEC EOL (<10%)")],
        "lines": [(90, WARNING, ":", "90% Enterprise Warning"),
                  (10, ERROR,   "--", "10% JEDEC EOL  |  JESD218B \u00a74.3")],
        "source": "JEDEC JESD218B \u00a74.3 \u2014 Available Spare Threshold",
    },
    "Endurance Used": {
        "spans": [(0,  60, SUCCESS, 0.07, "PASS (<60%)"),
                  (60, 90, WARNING, 0.09, "Late-Life Warning"),
                  (90, 100, ERROR,  0.20, "FAIL \u2014 RMA Required (>90%)")],
        "lines": [(60, WARNING, ":", "60% Late-Life Threshold"),
                  (90, ERROR,   "--", "90% RMA Threshold  |  JESD218B")],
        "source": "JEDEC JESD218B \u2014 Percentage Used / Endurance Indicator",
    },
    "Media Errors": {
        "spans": [(0, 0.5, SUCCESS, 0.07, "PASS (0 errors)"),
                  (0.5, 200, ERROR,  0.18, "FAIL \u2014 Any UECC = Immediate RMA")],
        "lines": [(0.5, ERROR, "--", "FAIL Threshold \u2014 NVMe UECC > 0")],
        "source": "NVMe Spec 1.4c \u00a75.14 \u2014 Media & Data Integrity Errors (UECC)",
    },
    "Unsafe Shutdowns": {
        "spans": [(0,   50,    SUCCESS, 0.07, "PASS (<50)"),
                  (50,  500,   WARNING, 0.09, "Elevated (50\u2013500)"),
                  (500, 99999, ERROR,   0.15, "FAIL \u2014 PSU/Infrastructure (>500)")],
        "lines": [(50,  WARNING, ":",  "50 \u2014 Elevated Baseline"),
                  (500, ERROR,   "--", "500 \u2014 Infrastructure FAIL")],
        "source": "Enterprise power stability baseline  |  NVMe SMART",
    },
    "Error Log Entries": {
        "spans": [],   # log-scale chart \u2014 lines only
        "lines": [(100,  WARNING, ":",  "100 \u2014 Elevated FW Errors"),
                  (1000, ERROR,   "--", "1,000 \u2014 Controller FAIL Threshold")],
        "source": "NVMe Spec 1.4c \u00a75.14 \u2014 Error Log Entry Count",
    },
    "Power On Hours": {
        "spans": [(0,      8760,  SUCCESS, 0.06, "Year 1  (Infant Mortality Zone)"),
                  (8760,  35040,  SUCCESS, 0.04, "Year 1\u20134  (Stable Life)"),
                  (35040, 87600,  WARNING, 0.08, "Year 4\u201310  (Aging Zone)"),
                  (87600, 999999, ERROR,   0.14, "Year 10+  (Wear-Out Zone)")],
        "lines": [(35040, WARNING, ":", "4 Years POH  (Aging Starts)"),
                  (87600, ERROR,   "--", "10 Years POH  (Wear-Out)")],
        "source": "Bathtub Reliability Curve \u2014 enterprise MTBF 1\u20132M hrs",
    },
}

sns.set_theme(style="darkgrid", context="paper")
plt.rcParams.update({
    "figure.facecolor": BG_CARD, "axes.facecolor": BG_CARD, "savefig.facecolor": BG_CARD,
    "axes.edgecolor": FG_SUB,    "axes.labelcolor": FG_TEXT,  "xtick.color": FG_SUB,
    "ytick.color": FG_SUB,       "text.color": FG_TEXT,       "grid.color": "#334155",
    "font.family": "Segoe UI",
})

# ======================================================================
#  INSIGHTS KNOWLEDGE BASE  (5-section per chart)
# ======================================================================
INSIGHTS = {

    "scorecard": {
        "title": "POOL MISSION-READINESS SCORECARD  (0-100 INDEX)",
        "FOR EVERYONE":
            "This number is the overall grade for your entire batch of SSDs. Think of it like a school report card for your storage hardware. "
            "100 is perfect — all drives healthy. If it drops below 70, something needs your attention today. The gauge fills left-to-right and "
            "turns red as the score falls.",
        "HOW TO READ":
            "Green (90-100) = Go. Yellow (70-89) = Caution. Red (below 70) = Stop and act. The score combines data from every sensor across every "
            "drive in the batch into one executive signal. Hover your eye across the KPI banner cards at the top — Total Drives, Critical/RMA count, "
            "Warnings, and Estimated MTBF give you the raw numbers behind this score.",
        "KEY THRESHOLDS":
            "90-100 → Optimal. Continue routine operations and archive results.  |  "
            "70-89  → Caution. Review all WARNING-flagged drives this week.  |  "
            "50-69  → Risk. Engineering escalation required immediately.  |  "
            "Below 50 → Emergency. Initiate mass RMA review and data migration now.",
        "EXPERT NOTES":
            "Score formula: 100 − (ERROR_rate × 50) − (WARN_rate × 20) − max(0, (avg_temp − 40) × 0.5). "
            "Penalty weights are chosen so a pool with 1% ERROR drives loses ~5 points, and a pool averaging 60°C loses ~10 points. "
            "This KPI is optimized for RMA batch audit cycles. For continuous production SLA monitoring, supplement with the MTBF figure "
            "and Cpk index from the QC/Sigma tab to get JEDEC 218B-aligned reliability confidence.",
        "ACTION GUIDE":
            "Score > 90: Log and archive — no action needed.  |  "
            "Score 70-90: Pull WARNING drives for physical inspection; cross-reference with the Inventory tab Serial column.  |  "
            "Score < 70: Trigger formal RMA escalation. Export the RMA Priority List from Dataset Hub and file with your vendor immediately.",
    },

    "reliability": {
        "title": "FIELD RELIABILITY STRATIFICATION  (WEAR vs. POWER-ON HOURS)",
        "FOR EVERYONE":
            "Each dot is one SSD drive. The further right it sits, the longer the drive has been running. The higher it sits, the more worn out it is. "
            "A healthy pool has all its dots following a diagonal — older drives are more worn, younger drives are fresh. Dots that are far above "
            "their peers at the same age are burning through their lifespan dangerously fast.",
        "HOW TO READ":
            "Start at the bottom-left (new, fresh drives) and trace diagonally up-right — that is normal aging. "
            "Any dot sitting much higher than others at the same horizontal position (same age) is a workload outlier — it is wearing out faster than expected. "
            "Color-coding separates drive model families so you can compare NVMe vs. SATA vs. QLC aging rates directly.",
        "KEY THRESHOLDS":
            "Endurance > 80% with POH < 5,000  → Critical outlier, investigate immediately.  |  "
            "Endurance > 50% with POH > 20,000 → Acceptable aging for high-workload drives.  |  "
            "Endurance < 10% with POH > 40,000 → End-of-life imminent, plan replacement.  |  "
            "Flat cluster near 0% endurance    → Fresh, lightly-used pool — excellent.",
        "EXPERT NOTES":
            "The slope of a linear regression on this scatter gives the Workload Intensity Coefficient (WIC): Δ(Endurance%)/Δ(POH). "
            "Enterprise NVMe TLC should show WIC < 0.003 %/hr at rated 3-DWPD workloads. QLC drives exhibit slopes 1.5-2× higher by design. "
            "Outliers above the regression line indicate WAF (Write Amplification Factor) exceeding rated spec — typically caused by "
            "unoptimized random-write patterns, absence of garbage-collection scheduling, or DRAM-less controller behavior under mixed I/O.",
        "ACTION GUIDE":
            "Identify dots far above the trend line. Find their Serial in the Inventory tab, then use Source_Path to locate the originating test station or rack. "
            "High-WAF outliers need host-side I/O pattern analysis: check for fragmented writes, or drives assigned to use cases mismatched to their NAND type "
            "(e.g., QLC in a write-heavy OLTP role). Consider switching those positions to TLC or SLC-cached enterprise drives.",
    },

    "composition": {
        "title": "EXECUTIVE POOL HEALTH ALLOCATION  (STATUS DISTRIBUTION)",
        "FOR EVERYONE":
            "This donut chart shows the split between healthy (green), warning (yellow), and failed (red) drives in your batch. "
            "The bigger the green slice, the better. Any red means hardware has failed and needs replacement. "
            "Even a tiny red slice on an enterprise deployment is a problem worth fixing today.",
        "HOW TO READ":
            "Each colored wedge is a percentage of your total drive count. Green = all sensors nominal. "
            "Yellow = at least one sensor in the caution zone (high temp, low spare, or high wear). "
            "Red = critical failure — media errors detected or spare blocks exhausted. "
            "Read the percentage label on each slice, then multiply by total drive count to get the raw number.",
        "KEY THRESHOLDS":
            "Green (OK) target: > 99.9% for enterprise pools, > 95% for consumer/client batch testing.  |  "
            "Yellow (WARNING) acceptable: < 1% for new hardware, < 5% for aged pools.  |  "
            "Red (ERROR): Target is always 0%. Any non-zero value is a failed unit requiring RMA.",
        "EXPERT NOTES":
            "Status classification logic: ERROR = Media Errors > 0 (uncorrectable — silent data corruption risk). "
            "WARNING = Available Spare < 90% OR Temperature > 75°C. OK = all thresholds nominal. "
            "This multi-sensor fusion model elevates the entire drive's status on a single sensor breach. "
            "The 90% spare threshold is intentionally conservative vs. JEDEC JESD218 (which mandates only 10%) "
            "to give maximum data-integrity runway before the hardware reaches its absolute limit.",
        "ACTION GUIDE":
            "Any RED present → Export the RMA Priority List from Dataset Hub immediately. Do not ship these drives.  |  "
            "YELLOW > 5% → Check which threshold is being breached in the Trends tab. Isolate whether it is thermal, spare, or wear.  |  "
            "Pure-green pool on new hardware → Archive, approve for deployment.",
    },

    "temp_trend": {
        "title": "SEQUENTIAL THERMAL BATCH TRACKING  (TEMPERATURE vs. BATCH INDEX)",
        "FOR EVERYONE":
            "This line chart takes the temperature of every drive, one-by-one, down the batch. "
            "A flat, steady line between 35-55°C is a healthy, well-cooled pool. "
            "Big spikes upward mean specific drives — or groups of drives — are running dangerously hot. "
            "Multiple spikes in a row suggest an airflow problem in the rack, not individual drive failures.",
        "HOW TO READ":
            "X-axis = drive sequence number in the batch. Y-axis = temperature in Celsius. "
            "Trace the line left-to-right. A smooth, flat line is good. Single spikes = individual problem drive. "
            "Groups of elevated readings = rack thermal zone issue. "
            "Gradual upward drift = cooling system degradation over time.",
        "KEY THRESHOLDS":
            "Below 40°C  → Optimal (excellent cooling efficiency).  |  "
            "40-55°C     → Normal operating range — target zone.  |  "
            "55-70°C     → Caution zone — increase monitoring frequency.  |  "
            "70-75°C     → WARNING: throttling begins, NAND cell damage starts accumulating.  |  "
            "Above 75°C  → CRITICAL: JEDEC thermal protection threshold exceeded.",
        "EXPERT NOTES":
            "NVMe drives throttle PCIe bandwidth when NAND junction temperature exceeds 70°C (SMART Attribute 0xC2). "
            "Sustained operation above 65°C accelerates three failure mechanisms: SILC (Stress-Induced Leakage Current) in NAND charge-trap cells, "
            "electromigration in copper controller interconnects, and delamination of thermal interface material. "
            "The Arrhenius equation governs NAND degradation: every 10°C above rated operating temperature approximately doubles "
            "the electron trap density increase rate, exponentially accelerating bit-flip probability and UECC rates.",
        "ACTION GUIDE":
            "Isolated hot drive → Replace thermal pad/heatsink and retest.  |  "
            "Cluster of adjacent hot drives → Audit rack airflow: check blanking panel installation, cable routing obstruction, fan tray health.  |  "
            "Systematically elevated pool (all drives hot) → Datacenter thermal audit: CRAC unit capacity, hot/cold aisle containment integrity.",
    },

    "spare_trend": {
        "title": "NAND SPARE BLOCK CONSUMPTION TREND  (AVAILABLE SPARE vs. BATCH INDEX)",
        "FOR EVERYONE":
            "SSDs secretly keep a reserve of backup memory blocks — like a spare tire. "
            "This chart shows how full that reserve still is. 100% means the drive has its full emergency reserve available. "
            "Lower numbers mean the drive has been eating into its safety cushion to compensate for worn cells. "
            "Any drive below 90% is a warning sign; below 10% is an emergency.",
        "HOW TO READ":
            "Y-axis runs from 0 to 100%. A flat line near 100% is perfect. "
            "A downward sloping line means drives are depleting reserves — the FTL is retiring worn cells to the bad block table. "
            "Steep drops indicate aggressive workloads or drives that have been running much longer than others in the batch.",
        "KEY THRESHOLDS":
            "100-95% → Optimal. NAND wear-leveling working normally.  |  "
            "95-90%  → Mild depletion — acceptable for older drives.  |  "
            "90-50%  → WARNING: drive is consuming reserves at an elevated rate.  |  "
            "10-50%  → HIGH RISK: schedule data migration within 30 days.  |  "
            "Below 10% → JEDEC end-of-life threshold: drive may enter read-only mode at any time.",
        "EXPERT NOTES":
            "Available Spare reflects the FTL's (Flash Translation Layer) dynamic spare block pool. "
            "Depletion is driven by P/E cycle counts exceeding rated cell endurance (TLC: ~3,000 cycles; QLC: ~1,000 cycles), "
            "which causes cells to be retired to the bad block table. "
            "Depletion is non-linear — it accelerates as the pool shrinks, because the FTL has fewer resources to distribute wear leveling. "
            "Spare below 10% triggers 'guardband mode' on most controllers, which may force read-only state without warning, "
            "consistent with JEDEC JESD218B Section 4.3 end-of-life definition.",
        "ACTION GUIDE":
            "Spare < 90%: Flag for tracking. Cross-reference with workload intensity in the WIF chart (Analytics tab).  |  "
            "Spare < 50%: Schedule preemptive data migration within 30 days. Begin procurement for replacement.  |  "
            "Spare < 10%: Emergency data migration within 24 hours. Do NOT write new data. File RMA paperwork immediately.",
    },

    "wear_trend": {
        "title": "ENDURANCE CONSUMPTION BATCH HISTORY  (WEAR % vs. BATCH INDEX)",
        "FOR EVERYONE":
            "This chart shows how worn out each drive is, plotted in batch order — like checking the odometer reading on every vehicle in a pool. "
            "Low numbers = barely used, lots of life left. High numbers = approaching retirement. "
            "A gradual, steady increase across drives of mixed ages is expected. Sudden jumps are suspicious.",
        "HOW TO READ":
            "Y-axis = percentage of total rated lifespan consumed (0% = brand new, 100% = end of rated life). "
            "Drives of different ages will naturally show different readings — this is normal. "
            "Look specifically for: drives at 80%+ (need replacement planning), sudden vertical jumps "
            "(abnormal wear event), or groups of drives all clustering at high wear (pool aging out together).",
        "KEY THRESHOLDS":
            "0-30%   → Early life. Plenty of lifespan remaining.  |  "
            "30-60%  → Mid-life. Normal for drives 2-4 years in service.  |  "
            "60-80%  → Late life. Plan replacement in next budget cycle.  |  "
            "80-90%  → WARNING. Prioritize replacement within 6 months.  |  "
            "90-100% → CRITICAL. Drive may fail at any moment. Immediate replacement required.",
        "EXPERT NOTES":
            "Endurance Used % is derived from the NVMe SMART log page 0x02 'Percentage Used' field, normalized to a 0-100 consumed scale. "
            "JEDEC JESD218B defines this as the manufacturer's guaranteed minimum endurance — drives failing before this point qualify for warranty RMA. "
            "Key driver of wear is WAF (Write Amplification Factor). A WAF of 5× consumes endurance 5× faster than the workload spec implies. "
            "For QLC drives, expect ~1.5-2× faster wear progression vs. TLC equivalents due to smaller charge margin between cell states.",
        "ACTION GUIDE":
            "Wear > 80% in production: Budget for immediate replacement — do not wait for failure.  |  "
            "Sudden wear jumps (>10% spike): Investigate the specific drive's workload — likely unoptimized write pattern or TRIM disable.  |  "
            "All drives showing accelerated wear: Evaluate NAND type fit — QLC in write-heavy roles must be moved to TLC or SLC-cached enterprise variants.",
    },

    "media_trend": {
        "title": "NAND DATA INTEGRITY SEQUENTIAL TRACKING  (MEDIA ERRORS vs. BATCH INDEX)",
        "FOR EVERYONE":
            "Media errors are when a drive makes a mistake reading or writing data that it cannot fix itself — like a typo it cannot correct. "
            "The target is ZERO. Even a single media error on any drive is a serious problem that requires immediate replacement. "
            "This chart should be a completely flat line at zero for a healthy pool. Any spike is a failed drive.",
        "HOW TO READ":
            "The Y-axis should sit at 0 for all drives. Any spike or bar above zero is a drive that has experienced irreversible data integrity problems. "
            "The height of the spike is irrelevant — even 1 error is an automatic fail. "
            "Look across the entire line to confirm it never leaves zero.",
        "KEY THRESHOLDS":
            "0 errors          → Pass. Drive is data-safe.  |  "
            "1-10 errors       → CRITICAL FAILURE. RMA immediately. Data integrity cannot be trusted.  |  "
            "> 10 errors       → Severe. Quarantine drive. Verify all data written to it using external checksums.  |  "
            "IMPORTANT NOTE: These are UNCORRECTABLE errors — the drive's ECC has already failed to fix them.",
        "EXPERT NOTES":
            "NVMe SMART 'Media and Data Integrity Errors' counts specifically UNCORRECTABLE errors (UECC) — "
            "raw read failures that exceed the controller's LDPC/BCH correction capability. "
            "These are distinct from soft errors fixed by ECC in real-time (which are normal and not counted here). "
            "The enterprise threshold for UECC is absolute zero per JEDEC JESD218B. "
            "Media errors strongly correlate with three root causes: thermal overstress (SILC in charge-trap cells), "
            "NAND P/E cycle exhaustion, and data retention failure (charge leakage in unpowered cells held beyond rated retention time).",
        "ACTION GUIDE":
            "ANY non-zero count: Fail the drive. Export RMA list. Do NOT use for data storage.  |  "
            "Cluster of errors in adjacent batch positions: Check test equipment — bad test cable or corrupted test software may generate false positives.  |  "
            "Check the Correlation Matrix (Analytics tab): if Temp and Media Errors correlate > 0.7, thermal damage is the confirmed root cause.",
    },

    "shutdown_trend": {
        "title": "POWER PATH & INFRASTRUCTURE STABILITY  (UNSAFE SHUTDOWNS vs. BATCH INDEX)",
        "FOR EVERYONE":
            "Every time a drive loses power unexpectedly — like a server crash or power cut — it gets counted as an 'unsafe shutdown'. "
            "A few over a drive's lifetime is normal. Hundreds or thousands mean the server's power supply is unstable, "
            "and that instability is regularly threatening your data and the drives themselves.",
        "HOW TO READ":
            "Higher values = more unexpected power losses that drive has experienced over its lifetime. "
            "Low, consistent counts across all drives = stable power infrastructure — good. "
            "Some drives with much higher counts than others in the same batch = those specific drives were in servers with PSU issues. "
            "All drives high = systemic infrastructure problem (whole rack/datacenter).",
        "KEY THRESHOLDS":
            "0-50    → Nominal. Normal PDU switching events over years of operation.  |  "
            "50-500  → Elevated. Investigate PSU health and UPS protection coverage.  |  "
            "500-2000 → HIGH. Power stability failure. Infrastructure audit required.  |  "
            "> 2000  → CRITICAL. PSU failure or severely inadequate UPS protection. Immediate infrastructure repair.",
        "EXPERT NOTES":
            "Each unsafe shutdown increments when the drive loses power while holding dirty write buffers not yet committed to NAND. "
            "Enterprise NVMe drives with power-loss protection capacitors (PLPs) still increment this counter but protect data via capacitor-backed flush. "
            "Drives WITHOUT PLP (budget enterprise, most QLC designs) — each unsafe shutdown is a genuine data corruption risk. "
            "High counts correlate with accelerated NAND wear because each power cycle forces full controller state re-initialization, "
            "interrupting ongoing garbage collection and wear leveling cycles.",
        "ACTION GUIDE":
            "Count > 500: PSU audit required. Check PDU redundancy, UPS runtime, and power cable integrity at the rack level.  |  "
            "Specific drives with high counts: Cross-reference Source_Path in the Inventory tab — these came from known-bad servers.  |  "
            "High shutdown counts AND media errors on same drives: Clear causal chain — power instability → data corruption. RMA these drives.",
    },

    "log_trend": {
        "title": "CONTROLLER FIRMWARE EXCEPTION TRACKING  (ERROR LOG ENTRIES — LOG SCALE)",
        "FOR EVERYONE":
            "The firmware inside the SSD keeps a log of every error it encounters internally — like the 'check engine' codes on a car. "
            "A few codes over a long operating life is normal. Thousands suggest the controller has deep-seated problems. "
            "Note: this chart uses a logarithmic scale, so the gaps between grid lines represent 10× increases, not equal steps.",
        "HOW TO READ":
            "The Y-axis uses a log scale (each grid line = 10× the previous). This is intentional — it lets you see both "
            "drives with 5 errors and drives with 5,000 errors on the same chart. "
            "Look for drives that tower far above the rest — these are the problem children. "
            "A flat line near the bottom of the chart means a healthy, stable controller firmware.",
        "KEY THRESHOLDS":
            "0-100    → Normal. Routine operational events only.  |  "
            "100-1,000 → Elevated. Controller encountering recurring error conditions.  |  "
            "1,000-10,000 → HIGH. Controller instability. Consider firmware update or replacement.  |  "
            "> 10,000 → CRITICAL. Controller kernel-panic level errors. Drive is functionally unreliable.",
        "EXPERT NOTES":
            "NVMe Error Log Entries (NVMe Spec 1.4, Section 5.14.1) aggregate across multiple internal error classes: "
            "command timeouts, NVM subsystem failures, FTL exceptions, background operation errors, and controller state machine faults. "
            "Log-scale visualization is critical because the distribution follows a power law (Pareto): most drives cluster near zero, "
            "a small fraction have logarithmically higher counts. This is the classic 'long tail' in reliability engineering. "
            "Drives > 1,000 entries should be cross-referenced against firmware revision in the Firmware Maturity Matrix (QC/Sigma tab).",
        "ACTION GUIDE":
            "Drives > 1,000 log entries: Check firmware version in the Inventory tab.  |  "
            "Specific firmware revision driving elevated logs: File a firmware bug report with the vendor, include affected serials from the Inventory export.  |  "
            "High log count + media errors on same drive: Definitive controller failure pathway. RMA immediately, no further analysis needed.",
    },

    "correlation": {
        "title": "MASTER TELEMETRY CORRELATION MATRIX  (PEARSON r HEATMAP)",
        "FOR EVERYONE":
            "This color grid shows whether different drive health measurements affect each other. "
            "Dark blue means two things always rise or fall together. Dark red means when one rises, the other falls. "
            "White/light means no connection. This chart helps you find the ROOT CAUSE of problems — "
            "if heat and data errors are dark blue, heat is what is killing the drives.",
        "HOW TO READ":
            "Each row and column is one measurement. Find where two measurements cross and read the color. "
            "Dark blue (near 1.0) = strong positive link — they rise together. "
            "Dark red (near -1.0) = strong inverse link — one rises as the other falls. "
            "Near white (0.0) = no relationship. The diagonal is always 1.0 (a measurement always correlates with itself).",
        "KEY THRESHOLDS":
            "r > 0.7  → Strong positive relationship. Investigate causal link.  |  "
            "r 0.4-0.7 → Moderate relationship — monitor.  |  "
            "r < -0.7 → Strong inverse (expected: Spare vs. Endurance should be ~ -0.6 to -0.8).  |  "
            "Near 0.0 → Independent — these metrics do not drive each other.",
        "EXPERT NOTES":
            "Uses Pearson correlation coefficient r ∈ [-1, 1] for linear relationships. "
            "Sensitive to outliers — a few extreme drives can inflate or suppress r in small batches. "
            "For batches under 30 drives, interpret cautiously and prefer Spearman's rho for robustness. "
            "Key expected correlations: Endurance vs. POH (+0.5 to +0.8). Spare vs. Endurance (-0.4 to -0.8). "
            "Concerning correlations: Temp vs. Media Errors > +0.5 = thermal damage pathway. "
            "Unsafe Shutdowns vs. Error Log Entries > +0.6 = PSU-driven controller stress chain.",
        "ACTION GUIDE":
            "Print or screenshot this matrix. Any cell above 0.6 between a controllable variable (temperature, shutdowns) "
            "and a failure indicator (media errors, log entries) is your primary intervention target. "
            "Address the controllable variable first — reducing it will cascade improvements across all correlated failure modes.",
    },

    "thermal_density": {
        "title": "POOL THERMAL POPULATION DENSITY  (TEMPERATURE KDE BY DRIVE CLASS)",
        "FOR EVERYONE":
            "Instead of showing each drive individually, this chart shows where most drives cluster by temperature. "
            "A tall, narrow peak means very consistent temperatures — excellent cooling uniformity. "
            "A wide, flat spread means some areas are much hotter than others. "
            "A curve with two humps (bimodal) means you have two distinct thermal zones — usually a serious rack layout problem.",
        "HOW TO READ":
            "X-axis = temperature. Y-axis = density (how many drives are at that temperature). "
            "The peak of the curve shows the most common temperature for each drive class. "
            "Peaks centered below 50°C are ideal. Wide peaks indicate inconsistent cooling. "
            "Multiple humps per curve indicate mixed thermal environments in the same batch.",
        "KEY THRESHOLDS":
            "Peak centered at 35-50°C → Ideal datacenter cooling.  |  "
            "Peak at 50-60°C          → Acceptable but warm — review airflow optimization.  |  "
            "Peak above 60°C          → Systematic thermal management failure.  |  "
            "Bimodal distribution     → Hot spot in rack — check blanking panels, cable routing.  |  "
            "Curve spread > 15°C wide (σ > 5°C) → Inconsistent cooling — cold aisle containment may be compromised.",
        "EXPERT NOTES":
            "Kernel Density Estimate (KDE) with Scott's bandwidth rule (seaborn default). "
            "The area under the curve beyond 70°C is the fraction of the pool operating in the damage accumulation zone. "
            "For enterprise quality control, compare this distribution against the manufacturer's JEDEC 218A "
            "datasheet operating temperature range. "
            "Separate curves by Drive Class reveal whether specific NAND geometries (QLC vs. TLC) show different thermal signatures, "
            "which can indicate form-factor or controller TDP differences rather than cooling problems.",
        "ACTION GUIDE":
            "Single narrow peak below 50°C → No action.  |  "
            "Peak shifted right above 55°C → Upgrade cooling density or reduce drive packing density.  |  "
            "Bimodal distribution → Map hot drives back to physical rack positions via Source_Path in Inventory. "
            "Usually indicates missing blanking panels or cable obstruction.",
    },

    "wif_index": {
        "title": "WIF (WORKLOAD INTENSITY EFFICIENCY) REGRESSION  (DATA WRITTEN vs. ENDURANCE USED)",
        "FOR EVERYONE":
            "This scatter shows whether drives that write more data also wear out faster — which you would expect. "
            "But the KEY insight is efficiency: a drive in the bottom-right has written lots of data with little wear (champion). "
            "A drive in the top-left has barely written anything but shows high wear (terrible efficiency — it is destroying itself internally).",
        "HOW TO READ":
            "X-axis = endurance/wear percentage. Y-axis = total data written by the host. "
            "Drives bottom-right = high data written + low wear = efficient. "
            "Drives top-left = low data written + high wear = high WAF problem. "
            "The dashed regression line shows the pool average. Dots above it are inefficient; below it are efficient.",
        "KEY THRESHOLDS":
            "High data written + low wear  → Excellent (low WAF, well-optimized firmware and workload).  |  "
            "Low data written + high wear  → Poor (high WAF — likely random-write-heavy workload).  |  "
            "Steep regression line slope   → Pool-average WAF is elevated. Workload optimization needed.  |  "
            "Outliers far above line       → Individual drives with severe WAF. Investigate immediately.",
        "EXPERT NOTES":
            "True WAF = (NAND_bytes_written) / (Host_bytes_written), measurable via vendor-specific NVMe commands exposing "
            "NVMe SMART attributes 0xF1 (Host Write Commands) and 0xF2 (NAND Write Commands). "
            "This chart uses Data_Units_Written as host write proxy and Endurance_Used% as NAND write proxy. "
            "R² of the regression line indicates workload homogeneity: low R² = mixed workloads explain most endurance variance. "
            "Worst-case WAF can exceed 50× for highly fragmented small-block random writes (e.g., database journal files on QLC).",
        "ACTION GUIDE":
            "Drives above the regression line: Investigate the host application I/O pattern — likely fragmented sequential or small random writes.  |  "
            "High WAF across the pool: Enable OS TRIM/discard support, verify RAID controller is not blocking TRIM pass-through.  |  "
            "Specific models clustering above line: Check vendor firmware advisories for GC (garbage collection) bug fixes.",
    },

    "control_chart": {
        "title": "STATISTICAL PROCESS CONTROL — SHEWHART I-CHART  (TEMPERATURE)",
        "FOR EVERYONE":
            "This is a quality control tool borrowed from manufacturing. It plots temperatures in sequence and draws 'guardrail' lines "
            "to mark the normal variation range. Any dot outside the red dashed lines is a statistically unusual outlier — "
            "something abnormal happened with that specific drive or the test environment at that point in the batch.",
        "HOW TO READ":
            "The solid blue line connects each drive's temperature in batch order. "
            "The white dashed line is the average temperature. "
            "The red dashed lines are the Control Limits (±3 standard deviations). "
            "Any dot touching or crossing the red lines is a statistically anomalous event requiring investigation.",
        "KEY THRESHOLDS":
            "Points within ±1σ of center → Normal. Expected ~68% of points.  |  "
            "Points within ±2σ of center → Still normal. Expected ~95% of points.  |  "
            "1 point outside ±3σ limits  → Special cause variation — investigate this specific drive.  |  "
            "2-3 consecutive points > 2σ → Process drift — cooling is deteriorating.  |  "
            "7+ consecutive points on same side of centerline → Nelson Rule violation — systematic thermal bias.",
        "EXPERT NOTES":
            "Shewhart Individuals Control Chart (I-Chart): the ±3σ limits ensure only 0.27% of normally distributed points "
            "fall outside by chance (Type I error rate α = 0.0027). Points outside limits indicate 'special cause variation' "
            "vs. normal 'common cause variation'. This is the Measure phase output in Six Sigma DMAIC methodology. "
            "For small batches (n < 30), control limits are wider and less precise — interpret with care. "
            "Use Moving Range (MR) chart alongside for full SPC compliance per AIAG SPC Manual 2nd Edition.",
        "ACTION GUIDE":
            "Identify out-of-control points. Note their Batch_Idx. Cross-reference with the Inventory tab to find the drive Serial.  |  "
            "Check if out-of-control drives cluster in specific batch index ranges — reveals test environment issues (e.g., room temperature shift).  |  "
            "Multiple out-of-control points: Submit for formal root cause analysis using DMAIC Analyze phase tools.",
    },

    "capability": {
        "title": "PROCESS CAPABILITY (CPK ANALYSIS)  — THERMAL CONFORMANCE TO 70°C ENVELOPE",
        "FOR EVERYONE":
            "This chart shows how much safety margin your pool has against the 70°C danger threshold. "
            "The histogram shows where most temperatures fell. The red line is the danger limit. "
            "The further left the peak is from the red line, the more safety margin you have. "
            "The Cpk number on the chart is your safety score — higher is better (above 1.33 is the enterprise standard).",
        "HOW TO READ":
            "X-axis = temperature. Red vertical line at 70°C = upper specification limit (damage threshold). "
            "The histogram and KDE curve show the distribution of all drive temperatures. "
            "Cpk value: > 1.33 means you have a comfortable 4-sigma safety buffer. "
            "< 1.0 means your cooling process is failing to meet the design envelope.",
        "KEY THRESHOLDS":
            "Cpk > 2.0  → Six Sigma world-class (99.9997% conformance). Negligible thermal risk.  |  "
            "Cpk 1.67  → 5-Sigma level. Excellent — hyperscaler sign-off standard.  |  "
            "Cpk 1.33  → 4-Sigma. Standard enterprise target. Acceptable.  |  "
            "Cpk 1.0   → 3-Sigma. Marginal — improvement needed.  |  "
            "Cpk < 1.0 → Process incapable. Thermal management is FAILING the design envelope.",
        "EXPERT NOTES":
            "One-sided Cpk = (USL − x̄) / (3σ), where USL = 70°C. "
            "We use upper-only Cpk because there is no damaging lower temperature limit for SSDs (unlike semiconductor fabs). "
            "This differs from two-sided Cp capability. For JEDEC JESD218B thermal validation sign-off, "
            "many hyperscalers require Cpk ≥ 1.67 at the drive/slot level under maximum sustained workload. "
            "The histogram's right skew (long tail toward high temperatures) reveals thermal excursions "
            "that averaged metrics and the pool score would hide — always validate distribution shape, not just the mean.",
        "ACTION GUIDE":
            "Cpk > 1.67: Pass thermal validation. Document and sign off for deployment.  |  "
            "Cpk 1.0-1.67: Implement incremental cooling improvement (thermal paste, fan speed increase, reduced drive density) and re-test.  |  "
            "Cpk < 1.0: Halt testing. Full thermal audit required before proceeding. This batch cannot be certified.",
    },

    "pareto_2": {
        "title": "PARETO FAILURE MODE ANALYSIS  — THE VITAL FEW (80/20 RULE)",
        "FOR EVERYONE":
            "The Pareto principle states that 80% of problems are caused by 20% of issue types. "
            "This bar chart ranks failure types from most to least impactful. "
            "Fix the TALLEST bar first — that single action will eliminate more failures than anything else you can do. "
            "Ignore the small bars until the big ones are resolved.",
        "HOW TO READ":
            "Each bar is a different failure mode. The taller the bar, the more drives it affects. "
            "Bars are automatically sorted tallest-first (left to right). "
            "Read the Y-axis to get the count of drives affected by each mode. "
            "A bar at zero means that failure mode is not present in this batch — excellent.",
        "KEY THRESHOLDS":
            "Top mode affecting > 20% of pool → Pool-level systemic failure. Executive escalation required.  |  "
            "Top mode affecting 5-20%          → Process control issue — engineering review required.  |  "
            "Top mode affecting 1-5%           → Within normal RMA rates for most enterprise deployments.  |  "
            "All bars at zero                  → No failures detected. Perfect batch.",
        "EXPERT NOTES":
            "Based on Juran's Quality Control Pareto framework. Mode thresholds used: "
            "Media Errors (UECC > 0), Thermal (T > 75°C), Power (unsafe shutdowns > 500), "
            "Endurance (wear > 90%), Controller (log entries > 1,000), Spare (available spare < 95%). "
            "Thresholds are intentionally conservative vs. JEDEC minimums to catch at-risk drives before they reach the specification limit. "
            "In a mature quality process, this chart feeds directly into a Failure Mode and Effects Analysis (FMEA) worksheet "
            "and drives corrective action priorities in the DMAIC Improve phase.",
        "ACTION GUIDE":
            "Identify the tallest bar and execute its corrective action first.  |  "
            "Media or Thermal as top failure: Hardware or environment problems — engineering root cause required.  |  "
            "Spare Depletion as top: Mixed-age pool under high workload — upgrade NAND type or reduce write frequency.  |  "
            "Cross-reference: the drives counted in the Pareto bars are the same drives in the RMA Priority List export.",
    },

    "spare_margin": {
        "title": "POOL SPARE RESERVATION RUNWAY  (VIOLIN DISTRIBUTION BY DRIVE CLASS)",
        "FOR EVERYONE":
            "This violin chart shows the spread of spare-block percentages across different drive type families. "
            "Think of each violin as a shadow showing where most drives cluster vertically. "
            "Wide sections = lots of drives at that spare percentage. Narrow = few drives there. "
            "A healthy violin is wide at the top (near 100%) and tapers to nothing at the bottom.",
        "HOW TO READ":
            "Each violin shape is one drive model class. Wider = more drives at that spare level. "
            "White dot = median (middle value). Black bar = middle 50% of drives (IQR). "
            "Thin whisker lines extend to the full spread. "
            "A violin that extends far down toward 0% means at least some drives in that class are critically depleted.",
        "KEY THRESHOLDS":
            "Violin median > 95% → Healthy class.  |  "
            "Violin extending below 50% → Significant depletion in this class — monitor closely.  |  "
            "Any violin touching 0% → At least one drive at catastrophic failure threshold.  |  "
            "Wide violin at 100% → Consistent health across whole class.  |  "
            "Bimodal violin (two fat sections) → Two distinct age cohorts or workload subgroups in this class.",
        "EXPERT NOTES":
            "Violin = mirrored KDE. White dot = median. IQR bar spans 25th-75th percentile. Whiskers = 1.5× IQR. "
            "Points beyond whiskers are statistical outliers. "
            "For quality engineering, the lower whisker position is the critical metric — if it approaches 10% (JEDEC EOL), "
            "a significant fraction of the class is approaching failure. "
            "Bimodal shapes indicate mixed subpopulations (typically by age cohort or write-workload intensity) "
            "that should be separated for independent analysis rather than treated as one homogeneous group.",
        "ACTION GUIDE":
            "Class with median spare < 90%: Accelerate replacement planning for that model family.  |  "
            "High variance class (wide violin): Review workload assignment — drives are experiencing inconsistent usage patterns.  |  "
            "Tight, high violin classes: These are your most reliable performers — standardize future procurement on these models.",
    },

    "fw_parity": {
        "title": "FIRMWARE REVISION YIELD MATURITY  (STATUS BREAKDOWN BY FW VERSION)",
        "FOR EVERYONE":
            "This chart shows whether specific firmware versions (the internal software that runs each SSD) are causing problems. "
            "If a newer firmware version shows more red or yellow than the older one, that update introduced a bug — stop rolling it out. "
            "Each bar is one firmware version, split by how many drives on that version are healthy, warning, or failed.",
        "HOW TO READ":
            "Each stacked bar is a firmware revision (REV number). Green portion = OK drives. Yellow = WARNING. Red = ERROR. "
            "A bar that is almost entirely green is a good firmware revision. "
            "A bar with red or yellow that is larger than neighboring revisions = firmware regression. "
            "Compare bar colors left-to-right across revisions to spot regressions.",
        "KEY THRESHOLDS":
            "Revision with > 5% ERROR rate    → Potential firmware regression. Escalate to vendor.  |  "
            "Newer revision with more errors  → STOP THIS ROLLOUT IMMEDIATELY.  |  "
            "All revisions similar error rates → Firmware is not a causal factor — look elsewhere.  |  "
            "Single revision at 100% OK        → Candidate 'golden firmware' for pool standardization.",
        "EXPERT NOTES":
            "Normalized crosstab (index=firmware, columns=status) shows percentage yield per revision — a controlled experiment holding hardware constant. "
            "Statistical significance requires > 30 drives per firmware bar; small n makes percentages misleading. "
            "Common NVMe firmware regression symptoms: increased GC aggressiveness (higher WAF), broken power management states "
            "(increased unsafe shutdowns), relaxed ECC thresholds (apparent improvement masking NAND degradation), "
            "or SMART counter reset bugs (counters reset to 0, breaking trend analysis entirely).",
        "ACTION GUIDE":
            "Identify the revision with highest OK% — standardize the pool on this version.  |  "
            "Regression detected on specific revision: File a firmware bug report with affected drive serials (export from Inventory tab).  |  "
            "All revisions show similar error rates: Firmware is not root cause. Shift focus to thermal, workload, and hardware investigations.",
    },

    "anomaly_scatter": {
        "title": "MULTI-SENSOR ANOMALY DETECTION  (Z-SCORE > 3\u03c3 FLAGGING)",
        "FOR EVERYONE":
            "This chart automatically identifies drives that are statistically unusual compared to the rest of the pool. "
            "Blue dots are normal drives. Red stars are drives the algorithm has flagged as anomalous — "
            "their sensor readings deviate significantly from pool average across one or more metrics. "
            "Anomalous drives often predict failures before they become critical.",
        "HOW TO READ":
            "Blue dots = normal drives. Red stars = statistically flagged anomalies. "
            "The X-axis is temperature; the Y-axis is endurance/wear. "
            "Anomalies flagged in the top-right corner (hot AND worn) are the most urgent. "
            "Anomalies in unexpected positions (e.g., very high wear but low temperature) indicate non-thermal failure modes.",
        "KEY THRESHOLDS":
            "Z-score > 3.0 on any metric   → Definitive outlier. Investigation required.  |  "
            "Z-score 2.0-3.0               → Borderline anomaly. Track over time.  |  "
            "Multiple metrics flagged on same drive → High composite risk — escalate.  |  "
            "0 anomalies detected           → Pool is statistically homogeneous. Excellent.",
        "EXPERT NOTES":
            "Z-score = (x − μ) / σ per metric. Flagged if |z| > 3.0 (p < 0.0027 under normality). "
            "Composite Anomaly Score = sum of individual z-scores above threshold across all sensors. "
            "This is univariate per-metric detection — for true multivariate anomaly detection, "
            "Mahalanobis distance would be preferred but requires n > 3p (n = drives, p = metrics). "
            "Exported Anomaly Report from Dataset Hub includes the specific sensor flags that triggered each alert "
            "for efficient root-cause prioritization.",
        "ACTION GUIDE":
            "Any flagged drive: Cross-reference its Serial in the Inventory tab. Export Anomaly Report from Dataset Hub for vendor analysis.  |  "
            "Anomalies clustered in specific batch index ranges: Investigate temporal test conditions — batch-level defects or test equipment drift.  |  "
            "Anomaly count > 5% of pool: Revisit acceptance criteria — this batch may have manufacturing quality issues.",
    },

    "eol_prediction": {
        "title": "PREDICTIVE END-OF-LIFE (EOL) RUNWAY  (LINEAR WEAR EXTRAPOLATION)",
        "FOR EVERYONE":
            "This chart predicts when each drive will run out of usable lifespan, based on how fast it is wearing out right now. "
            "Drives with short bars have little time left — even if they are technically still working today. "
            "Use this chart to plan ahead: order replacements before drives fail, not after.",
        "HOW TO READ":
            "Each bar is one drive (labeled by serial number). Bar length = estimated days until 100% endurance is reached. "
            "Red bars = less than 90 days (emergency). Yellow bars = 90-365 days (plan now). Blue = over a year. "
            "Only the most at-risk drives are shown. Drives with > 2,000 days remaining are omitted for clarity.",
        "KEY THRESHOLDS":
            "< 90 days   → CRITICAL. Replace immediately. Start data migration today.  |  "
            "90-180 days → HIGH. Order replacements now. Schedule migration.  |  "
            "180-365 days → MEDIUM. Include in next budget cycle.  |  "
            "> 365 days  → LOW. Monitor quarterly.  |  "
            "NOTE: Actual failure may come earlier due to non-linear spare depletion acceleration near EOL.",
        "EXPERT NOTES":
            "Linear extrapolation: days_remaining = (100% − wear%) / (wear% / POH × 24). "
            "This is a conservative lower-bound estimate assuming constant workload intensity. "
            "Real drives exhibit non-linear wear acceleration near EOL due to spare block depletion increasing FTL write overhead. "
            "JEDEC JESD218B provides manufacturer-guaranteed minimum endurance — drives failing before this threshold qualify for warranty RMA. "
            "For mission-critical applications, plan replacement at 70% of predicted EOL to maintain the reliability floor. "
            "Export the full EOL table via Multi-Sheet Excel export in Dataset Hub for procurement planning with sorted urgency.",
        "ACTION GUIDE":
            "Drives < 90 days: Initiate emergency RMA or hot-spare replacement. Begin data migration immediately.  |  "
            "Drives 90-365 days: Add to procurement queue with at least 60-day lead time buffer.  |  "
            "Export Multi-Sheet Excel from Dataset Hub — the EOL Predictions sheet is sorted by urgency for direct use in procurement planning.",
    },

    "waf_chart": {
        "title": "WRITE AMPLIFICATION FACTOR (WAF) PROXY  — BY DRIVE CLASS",
        "FOR EVERYONE":
            "WAF stands for Write Amplification Factor. It measures whether a drive is efficiently using its rated lifespan. "
            "A WAF of 1 means the drive writes exactly what you give it to NAND. A WAF of 10 means it is writing 10x more to NAND than necessary — "
            "wearing itself out 10x faster. Lower is better. This chart compares WAF across drive model families.",
        "HOW TO READ":
            "Y-axis = WAF proxy value (lower = better, more efficient). Each group on the X-axis is a drive model class. "
            "The box shows the middle 50% of drives in that class. The line inside the box is the median. "
            "Whiskers extend to the full range. Dots above whiskers are outliers — the worst WAF drives in that class.",
        "KEY THRESHOLDS":
            "WAF < 2   → Excellent. Workload is well-matched to this drive type.  |  "
            "WAF 2-5   → Acceptable for mixed enterprise workloads.  |  "
            "WAF 5-10  → High. Workload optimization needed. Consider drive type change.  |  "
            "WAF > 10  → Critical write amplification. Wrong drive type for this workload.  |  "
            "NOTE: QLC drives have inherently higher WAF than TLC/SLC by design — context matters.",
        "EXPERT NOTES":
            "True WAF = (NAND_bytes_written) / (Host_bytes_written). This proxy normalizes Endurance_Used% "
            "against Data_Units_Written (scaled to pool-relative units). Not an absolute WAF but a valid relative comparison. "
            "True WAF requires NVMe attributes 0xF1 (Host Write) / 0xF2 (NAND Write), available on drives that expose them. "
            "Key WAF drivers: random small-block writes (kills sequential efficiency), disabled TRIM (prevents FTL dirty-block reclamation), "
            "insufficient over-provisioning, and controller-level compression inefficiency on incompressible data (encrypted or compressed data).",
        "ACTION GUIDE":
            "High WAF drive class: Evaluate whether this drive type is correct for the workload — QLC should be in sequential read-heavy roles only.  |  "
            "All classes showing high WAF: Enable OS TRIM/discard support; verify RAID/volume manager is not blocking TRIM pass-through.  |  "
            "WAF much higher for specific batch indices: Those drives may be mapped to different logical volumes with distinct write patterns.",
    },
}


# ======================================================================
#  SCROLLABLE FRAME
# ======================================================================
class ScrollableFrame(tk.Frame):
    def __init__(self, container, *args, **kwargs):
        super().__init__(container, *args, **kwargs)
        self.canvas = tk.Canvas(self, bg=BG_MAIN, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_window = tk.Frame(self.canvas, bg=BG_MAIN)
        self.scrollable_window.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_window, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda _: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda _: self.canvas.unbind_all("<MouseWheel>"))

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_canvas_configure(self, event):
        # Keep the embedded content frame matched to visible tab width.
        self.canvas.itemconfigure(self.canvas_window, width=event.width)


# ======================================================================
#  MAIN APPLICATION
# ======================================================================
class TriageIODesktop:
    def __init__(self, root):
        self.root = root
        self.root.title("TriageIO - Pool Telemetry Manager  v3.0")
        self.root.geometry("1680x1020")
        self.root.configure(bg=BG_MAIN)
        self.results_dir      = tk.StringVar(value="[ SELECT YOUR RESULTS FOLDER ]")
        self.processor        = None
        self.current_df       = None
        self.is_processing    = False
        self.rendered_tabs    = set()
        self.show_thresholds  = tk.BooleanVar(value=True)
        self._sort_state      = {}
        self.setup_styles()
        self.setup_ui()

    # ------------------------------------------------------------------
    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('default')
        style.configure("TNotebook",     background=BG_MAIN, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_CARD, foreground=FG_TEXT,
                        padding=[25, 10], font=('Segoe UI', 10, 'bold'))
        style.map("TNotebook.Tab", background=[("selected", ACCENT)])
        style.configure("Treeview",         background=BG_CARD, foreground=FG_TEXT,
                        fieldbackground=BG_CARD, rowheight=28)
        style.configure("Treeview.Heading", background=BG_MAIN, foreground=FG_TEXT,
                        font=('Segoe UI', 9, 'bold'))

    # ------------------------------------------------------------------
    def setup_ui(self):
        header = tk.Frame(self.root, bg=BG_CARD, height=80)
        header.pack(fill="x")
        tk.Label(header, text="\U0001f3db\ufe0f TRIAGEIO - POOL TELEMETRY MANAGER",
                 fg=FG_TEXT, bg=BG_CARD, font=("Segoe UI", 20, "bold")).pack(pady=20, side="left", padx=40)

        toolbar = tk.Frame(self.root, bg=BG_MAIN, pady=15)
        toolbar.pack(fill="x")
        tk.Label(toolbar, text="DATA SOURCE:", bg=BG_MAIN, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(40, 5))
        path_entry = tk.Entry(toolbar, textvariable=self.results_dir, width=80,
                 bg=BG_CARD, fg=FG_SUB, relief="flat", font=("Consolas", 10))
        path_entry.pack(side="left", padx=5)
        add_entry_menu(path_entry)
        tk.Button(toolbar, text="BROWSE FOLDER", command=self.browse_dir,
                  bg=BG_CARD, fg="white", relief="flat", padx=20,
                  font=("Segoe UI", 9, "bold")).pack(side="left", padx=5)
        tk.Button(toolbar, text="\U0001f504 REFRESH MASTER ANALYSIS", bg=SUCCESS, fg="white",
                  relief="flat", padx=30, font=("Segoe UI", 10, "bold"),
                  command=self.run_analysis).pack(side="right", padx=20)
        # Pass/Fail threshold toggle
        thresh_btn = tk.Checkbutton(
            toolbar, text="[ZONES] PASS/FAIL",
            variable=self.show_thresholds, command=self._toggle_thresholds,
            bg=BG_MAIN, fg=WARNING, selectcolor=BG_CARD,
            activebackground=BG_MAIN, activeforeground=WARNING,
            font=("Segoe UI", 9, "bold"), indicatoron=True,
        )
        thresh_btn.pack(side="right", padx=10)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=30, pady=10)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.tab_exec      = ScrollableFrame(self.notebook)
        self.tab_trends    = ScrollableFrame(self.notebook)
        self.tab_analytics = ScrollableFrame(self.notebook)
        self.tab_qc        = ScrollableFrame(self.notebook)
        self.tab_actions   = ScrollableFrame(self.notebook)
        self.tab_grid      = tk.Frame(self.notebook, bg=BG_MAIN)

        for tab, name in [
            (self.tab_exec,      "\U0001f4ca EXECUTIVE"),
            (self.tab_trends,    "\U0001f4c8 TRENDS"),
            (self.tab_analytics, "\U0001f52c ANALYTICS"),
            (self.tab_qc,        "\u2696\ufe0f QUALITY/SIGMA"),
            (self.tab_actions,   "\U0001f6e0\ufe0f DATASET HUB"),
            (self.tab_grid,      "\U0001f4cb INVENTORY"),
        ]:
            self.notebook.add(tab, text=name)

        self.setup_grid_widgets()
        self.show_welcome_msg()

    # ------------------------------------------------------------------
    def setup_grid_widgets(self):
        cols = ('Batch_Idx', 'Drive Class', 'Interface', 'Model', 'Serial', 'Firmware',
                'Temp °C', 'Spare %', 'Endurance %', 'Media Err', 'Shutdowns',
                'Log Entries', 'POH', 'Data Written', 'Status')
        self.tree = ttk.Treeview(self.tab_grid, columns=cols, show='headings')
        widths = {
            'Batch_Idx': 55, 'Drive Class': 130, 'Interface': 115, 'Model': 185,
            'Serial': 115, 'Firmware': 70, 'Temp °C': 65, 'Spare %': 72,
            'Endurance %': 90, 'Media Err': 80, 'Shutdowns': 85,
            'Log Entries': 85, 'POH': 75, 'Data Written': 100, 'Status': 80,
        }
        for c in cols:
            self.tree.heading(c, text=c, command=lambda _c=c: self.sort_grid(_c))
            self.tree.column(c, width=widths.get(c, 100), anchor="center")
        vsb = ttk.Scrollbar(self.tab_grid, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(self.tab_grid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self.tree.tag_configure('warning', background='#451a03', foreground=WARNING)
        self.tree.tag_configure('error',   background='#450a0a', foreground=ERROR)
        self.tree.bind("<Button-3>", self.show_tree_menu)

    def show_tree_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            menu = tk.Menu(self.tree, tearoff=0)
            menu.add_command(label="Copy Row", command=self.copy_tree_row)
            menu.add_command(label="Copy Serial", command=lambda: self.copy_tree_cell('Serial'))
            menu.post(event.x_root, event.y_root)

    def copy_tree_row(self):
        selected_item = self.tree.selection()[0]
        values = self.tree.item(selected_item)['values']
        self.root.clipboard_clear()
        self.root.clipboard_append("\t".join(map(str, values)))

    def copy_tree_cell(self, col_name):
        selected_item = self.tree.selection()[0]
        cols = self.tree.cget('columns')
        idx = list(cols).index(col_name)
        val = self.tree.item(selected_item)['values'][idx]
        self.root.clipboard_clear()
        self.root.clipboard_append(str(val))

    def sort_grid(self, col):
        rows = self.tree.get_children('')
        if not rows:
            return

        def parse_numeric(value):
            s = str(value).strip().replace(",", "")
            m = re.search(r"-?\d+(?:\.\d+)?", s)
            return float(m.group(0)) if m else None

        def sort_key(item):
            raw = item[0]
            num = parse_numeric(raw)
            if num is not None:
                return (0, num)
            return (1, str(raw).lower())

        items = [(self.tree.set(k, col), k) for k in rows]
        asc = not self._sort_state.get(col, False)
        items.sort(key=sort_key, reverse=not asc)
        self._sort_state[col] = asc
        for i, (_, k) in enumerate(items):
            self.tree.move(k, '', i)

    # ------------------------------------------------------------------
    #  INSIGHT PANEL RENDERER
    # ------------------------------------------------------------------
    def render_block(self, container, fig, insight_key):
        """Embeds a matplotlib figure then renders the 5-section insight panel below it."""
        block = tk.Frame(container, bg=BG_MAIN, pady=30)
        block.pack(fill="x", padx=40)
        canvas = FigureCanvasTkAgg(fig, master=block)
        canvas.draw_idle()
        canvas.get_tk_widget().pack(fill="x")
        self._render_insight_panel(block, insight_key)

    def _render_insight_panel(self, container, insight_key):
        insight = INSIGHTS.get(insight_key)
        if not insight:
            return
        panel = tk.Frame(container, bg=BG_CARD, padx=45, pady=30,
                         highlightbackground=ACCENT, highlightthickness=1)
        panel.pack(fill="x", pady=(15, 0))
        title = insight.get("title", insight_key.replace("_", " ").upper())
        tk.Label(panel, text=f"\U0001f4d8  {title}", font=("Segoe UI", 14, "bold"),
                 fg=ACCENT, bg=BG_CARD).pack(anchor="w", pady=(0, 12))
        body_labels = []
        for section, color in SEC_COLORS.items():
            text = insight.get(section, "")
            if not text:
                continue
            tk.Label(panel, text=f"\u25b8  {section}", font=("Segoe UI", 10, "bold"),
                     fg=color, bg=BG_CARD).pack(anchor="w", pady=(10, 2))
            body = tk.Label(
                panel,
                text=text,
                font=("Segoe UI", 10),
                fg=FG_TEXT,
                bg=BG_CARD,
                justify="left",
                anchor="w",
            )
            body.pack(anchor="w", padx=(20, 0), pady=(0, 4), fill="x")
            body_labels.append(body)

        def _update_wraplength(event=None):
            # Keep paragraphs readable and fully visible without per-item scrolling.
            wrap = max(420, panel.winfo_width() - 110)
            for lbl in body_labels:
                lbl.configure(wraplength=wrap)

        panel.bind("<Configure>", _update_wraplength)
        panel.after_idle(_update_wraplength)

    # ------------------------------------------------------------------
    #  WELCOME
    # ------------------------------------------------------------------
    def show_welcome_msg(self):
        c = self.tab_exec.scrollable_window
        for w in c.winfo_children():
            w.destroy()
        f = tk.Frame(c, bg=BG_MAIN)
        f.pack(expand=True, pady=250)
        tk.Label(f, text="\U0001f3db\ufe0f TRIAGEIO POOL TELEMETRY ENGINE ACTIVE",
                 font=("Segoe UI", 32, "bold"), fg=FG_TEXT, bg=BG_MAIN).pack()
        tk.Label(f, text="Select a telemetry root folder to initiate master recursive analysis.",
                 fg=FG_SUB, bg=BG_MAIN, font=("Segoe UI", 16)).pack(pady=25)

    # ------------------------------------------------------------------
    def browse_dir(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = filedialog.askdirectory(initialdir=base_dir, title="\U0001f4c1 SELECT ROOT")
        if d:
            self.results_dir.set(d)
            self.run_analysis()

    # ------------------------------------------------------------------
    def run_analysis(self):
        if self.is_processing:
            return
        p = self.results_dir.get()
        if not os.path.isdir(p):
            self._show_status("No valid folder selected.", ERROR)
            return
        
        self.is_processing = True
        self._show_loading()
        
        # Run heavy processing in background thread
        thread = threading.Thread(target=self._processing_worker, args=(p,), daemon=True)
        thread.start()

    def _processing_worker(self, p):
        """Heavy lifting done here in background thread."""
        try:
            # CRITICAL: Matplotlib is not thread-safe. Do NOT call plt.close here.
            self.processor  = TelemetryProcessor(p)
            df = self.processor.process_files()
            self.root.after(0, self._handle_processing_result, df, None)
        except Exception as e:
            self.root.after(0, self._handle_processing_result, None, e)

    def _handle_processing_result(self, df, error):
        """Finalize UI on main thread after processing is done."""
        self.is_processing = False
        self.current_df = df
        self.rendered_tabs.clear()
        
        # Safely close figures on main thread
        plt.close('all')

        if error:
            self._show_inline_error(self.tab_exec.scrollable_window,
                                    "File Processing Failed", str(error))
            return

        if self.current_df is None or self.current_df.empty:
            self._show_inline_error(self.tab_exec.scrollable_window,
                                    "No Data Found",
                                    "No valid .xlsx telemetry files were found in the selected folder.")
            return

        self._populate_grid()
        self._render_active_tab()

    def _on_tab_changed(self, event):
        """Lazy loader: only render the tab when it becomes active."""
        if self.current_df is not None:
            self._render_active_tab()

    def _render_active_tab(self):
        try:
            tab_id = self.notebook.select()
            if not tab_id:
                return
            idx = self.notebook.index(tab_id)
            
            # Use the children of the notebook to track rendered state
            tabs = self.notebook.winfo_children()
            if idx >= len(tabs):
                return
            tab_obj = tabs[idx]
            
            if tab_obj in self.rendered_tabs:
                return

            render_map = {
                0: self.render_exec_tab,
                1: self.render_trends_tab,
                2: self.render_analytics_tab,
                3: self.render_qc_tab,
                4: self.render_actions_hub,
            }
            
            if idx in render_map:
                self.root.config(cursor="watch")
                self.root.update_idletasks()
                self.rendered_tabs.add(tab_obj)
                render_map[idx]()
                self.root.config(cursor="")
        except Exception as e:
            self.root.config(cursor="")
            print(f"LAZY RENDER ERROR: {e}")

    def _populate_grid(self):
        try:
            for i in self.tree.get_children():
                self.tree.delete(i)
            for _, r in self.current_df.iterrows():
                tag = r['Overall Status'].lower() if r['Overall Status'] != 'OK' else ''
                duw_gb = r['Data Units Written'] * 512 / 1e9  # 512-byte units → GB
                duw_str = f"{duw_gb:,.0f} GB" if duw_gb < 1000 else f"{duw_gb/1000:.1f} TB"
                self.tree.insert('', 'end', values=(
                    r['Batch_Idx'],
                    r['Drive Class'],
                    r.get('Interface', 'Unknown'),
                    r['Model'],
                    r['Serial'],
                    r['Firmware'],
                    f"{int(r['Temperature'])}\u00b0C",
                    f"{int(r['Available Spare'])}%",
                    f"{int(r['Endurance Used'])}%",
                    int(r['Media Errors']),
                    int(r['Unsafe Shutdowns']),
                    int(r['Error Log Entries']),
                    f"{int(r['Power On Hours']):,}",
                    duw_str,
                    r['Overall Status'],
                ), tags=(tag,))
        except Exception as e:
            self._show_inline_error(self.tab_grid,
                                    "Inventory Grid Error", str(e))

    def _show_loading(self):
        """Shows a loading indicator in all primary tabs."""
        for tab in [self.tab_exec, self.tab_trends, self.tab_analytics, self.tab_qc, self.tab_actions]:
            container = tab.scrollable_window
            for w in container.winfo_children():
                w.destroy()
            f = tk.Frame(container, bg=BG_MAIN)
            f.pack(expand=True, pady=200)
            
            # Simple loading animation placeholder
            tk.Label(f, text="\U0001f504", font=("Segoe UI", 48), fg=ACCENT, bg=BG_MAIN).pack()
            tk.Label(f, text="ANALYZING TELEMETRY POOL...", 
                     font=("Segoe UI", 14, "bold"), fg=FG_TEXT, bg=BG_MAIN).pack(pady=20)
            tk.Label(f, text="Correlating sensor data and generating risk matrices...", 
                     font=("Segoe UI", 10), fg=FG_SUB, bg=BG_MAIN).pack()

    # ------------------------------------------------------------------
    def _show_status(self, msg, color=None):
        """Non-modal status message shown in the executive tab."""
        color = color or FG_SUB
        container = self.tab_exec.scrollable_window
        for w in container.winfo_children():
            w.destroy()
        f = tk.Frame(container, bg=BG_MAIN)
        f.pack(expand=True, pady=200)
        SelectableLabel(f, text=msg, font=("Segoe UI", 18), fg=color, bg=BG_MAIN,
                 justify="center", anchor="center").pack()

    def _show_inline_error(self, container, title, detail):
        """
        Renders a styled inline error card inside a tab's scrollable window
        instead of popping a modal dialog. Non-blocking.
        """
        # If container is a ScrollableFrame, target its inner window
        target = getattr(container, 'scrollable_window', container)
        for w in target.winfo_children():
            w.destroy()
        outer = tk.Frame(target, bg=BG_MAIN)
        outer.pack(expand=True, pady=120, padx=80, fill="x")
        card = tk.Frame(outer, bg=BG_CARD, padx=50, pady=40,
                        highlightbackground=ERROR, highlightthickness=2)
        card.pack(fill="x")
        tk.Label(card, text="\u26a0\ufe0f  " + title,
                 font=("Segoe UI", 16, "bold"), fg=ERROR, bg=BG_CARD).pack(anchor="w")
        SelectableLabel(card, text=detail, font=("Segoe UI", 11), fg=FG_TEXT,
                 bg=BG_CARD).pack(anchor="w", pady=(16, 0), fill="x")

    # ==================================================================
    #  TAB: EXECUTIVE
    # ==================================================================
    def render_exec_tab(self):
        container = self.tab_exec.scrollable_window
        for w in container.winfo_children():
            w.destroy()
        analyzer = TelemetryAnalyzer(self.current_df)
        score    = analyzer.calculate_fleet_score()
        color    = SUCCESS if score > 85 else (WARNING if score > 70 else ERROR)
        mtbf     = analyzer.estimate_mtbf()
        total    = len(self.current_df)
        n_err    = len(self.current_df[self.current_df['Overall Status'] == 'ERROR'])
        n_warn   = len(self.current_df[self.current_df['Overall Status'] == 'WARNING'])
        mtbf_str = f"{int(mtbf):,} hrs" if mtbf != float('inf') else "\u221e  (No Failures)"

        # KPI Banner
        kpi_frame = tk.Frame(container, bg=BG_MAIN)
        kpi_frame.pack(fill="x", padx=40, pady=(30, 5))
        for label, value, c in [
            ("TOTAL DRIVES",    str(total),   ACCENT),
            ("CRITICAL / RMA",  str(n_err),   ERROR   if n_err  > 0 else SUCCESS),
            ("WARNINGS",        str(n_warn),  WARNING if n_warn > 0 else SUCCESS),
            ("EST. POOL MTBF", mtbf_str,     FG_TEXT),
        ]:
            card = tk.Frame(kpi_frame, bg=BG_CARD, padx=40, pady=20,
                            highlightbackground=c, highlightthickness=2)
            card.pack(side="left", expand=True, fill="x", padx=10)
            tk.Label(card, text=label, font=("Segoe UI", 9, "bold"), fg=FG_SUB, bg=BG_CARD).pack()
            tk.Label(card, text=value, font=("Segoe UI", 24, "bold"), fg=c, bg=BG_CARD).pack()

        # Gauge + scorecard insight
        fig_gauge = self._make_gauge_fig(score, color)
        self.render_block(container, fig_gauge, "scorecard")

        # Reliability Stratification
        fig1, ax1 = plt.subplots(figsize=(15, 6))
        # Threshold overlays — endurance (h) and POH (v)
        self._add_threshold_overlay(ax1, "Endurance Used",  orient='h', add_legend=False)
        self._add_threshold_overlay(ax1, "Power On Hours",  orient='v', add_legend=False)
        # Color individual points by status (pass/warn/fail) when zones are on
        if self.show_thresholds.get():
            status_palette = {'OK': SUCCESS, 'WARNING': WARNING, 'ERROR': ERROR}
            for status, grp in self.current_df.groupby('Overall Status'):
                ax1.scatter(grp['Power On Hours'], grp['Endurance Used'],
                            c=status_palette.get(status, ACCENT),
                            s=130, alpha=0.85, label=f"Status: {status}",
                            edgecolors='white', linewidths=0.4, zorder=4)
        else:
            sns.scatterplot(data=self.current_df, x="Power On Hours", y="Endurance Used",
                            hue="Drive Class", s=120, ax=ax1, zorder=4)
        if self.current_df['Power On Hours'].nunique() > 1:
            z = np.polyfit(self.current_df['Power On Hours'], self.current_df['Endurance Used'], 1)
            xs = np.linspace(self.current_df['Power On Hours'].min(), self.current_df['Power On Hours'].max(), 100)
            ax1.plot(xs, np.poly1d(z)(xs), color=FG_SUB, ls="--", lw=1.5, label="Trend Line", zorder=5)
        if self.show_thresholds.get():
            ax1.annotate("Thresholds: JEDEC JESD218B (Endurance)  |  Bathtub Curve (POH)",
                         xy=(0.01, 0.01), xycoords='axes fraction', fontsize=7, color=FG_SUB)
        ax1.set_title("Reliability Stratification \u2014 Wear vs. Power-On Hours"
                      + ("  ([ZONES] PASS/FAIL BY STATUS)" if self.show_thresholds.get() else ""))
        ax1.set_xlabel("Power On Hours  (cumulative hours the drive has been powered on)")
        ax1.set_ylabel("Endurance Used  (% — 0% = new,  100% = end of rated life)")
        # Keep POH axis focused on pool data (threshold zones include very large spans).
        poh_min = float(self.current_df['Power On Hours'].min())
        poh_max = float(self.current_df['Power On Hours'].max())
        poh_pad = (poh_max - poh_min) * 0.08 if poh_max > poh_min else 500.0
        ax1.set_xlim(max(0.0, poh_min - poh_pad), poh_max + poh_pad)
        ax1.xaxis.set_major_formatter(StrMethodFormatter('{x:,.0f}'))
        apply_smart_limits(ax1, self.current_df['Endurance Used'], 'Endurance Used')
        ax1.legend(fontsize=8, framealpha=0.7)
        self.render_block(container, fig1, "reliability")

        # Health Composition Donut
        fig2, ax2 = plt.subplots(figsize=(15, 6))
        status_map = self.current_df['Overall Status'].value_counts()
        palette    = {s: c for s, c in [('OK', SUCCESS), ('WARNING', WARNING), ('ERROR', ERROR)]}
        colors_pie = [palette.get(s, FG_SUB) for s in status_map.index]
        total = float(status_map.sum()) if len(status_map) else 1.0
        outside_labels = [
            f"{status}  ({(count / total) * 100:.1f}%)"
            for status, count in status_map.items()
        ]
        wedges, texts = ax2.pie(
            status_map,
            labels=outside_labels,
            labeldistance=1.20,
            wedgeprops={'width': 0.45},
            colors=colors_pie,
            startangle=90,
        )
        for t in texts:
            t.set_fontsize(11)
        ax2.set_title("Pool Health Composition — Status Allocation")
        self.render_block(container, fig2, "composition")

    # ==================================================================
    #  TAB: TRENDS
    # ==================================================================
    def render_trends_tab(self):
        container = self.tab_trends.scrollable_window
        for w in container.winfo_children():
            w.destroy()
        METRIC_YLABELS = {
            "Temperature":       "Temperature  (°C — 40–55°C normal, 70°C = JEDEC limit)",
            "Available Spare":   "Available Spare  (% — 10% = JEDEC end-of-life)",
            "Endurance Used":    "Endurance Used  (% — 0% = new,  100% = end of rated life)",
            "Media Errors":      "Media Errors  (UECC count — 0 = pass,  any non-zero = RMA)",
            "Unsafe Shutdowns":  "Unsafe Shutdowns  (lifetime count of unexpected power losses)",
            "Error Log Entries": "Error Log Entries  (firmware exception count — log scale)",
        }
        trends = [
            ("Temperature",      "Thermal Batch Tracking",         "temp_trend"),
            ("Available Spare",  "NAND Spare Consumption",         "spare_trend"),
            ("Endurance Used",   "Wear-out Batch History",         "wear_trend"),
            ("Media Errors",     "Bit-Flip Sequential Tracking",   "media_trend"),
            ("Unsafe Shutdowns", "Infrastructure Stability",       "shutdown_trend"),
            ("Error Log Entries","Firmware Exception Tracking",    "log_trend"),
        ]
        for metric, title, key in trends:
            fig, ax = plt.subplots(figsize=(15, 5))
            # Draw threshold zones FIRST (behind data)
            self._add_threshold_overlay(ax, metric, orient='h')
            sns.lineplot(data=self.current_df, x="Batch_Idx", y=metric,
                         hue="Drive Class", marker="o", errorbar=None, ax=ax, zorder=3)
            if metric == "Error Log Entries":
                ax.set_yscale('log')
            else:
                apply_smart_limits(ax, self.current_df[metric], metric)

            suffix = "  ([ZONES] PASS/FAIL ON)" if self.show_thresholds.get() else ""
            ax.set_title(f"{title}{suffix}")
            ax.set_xlabel("Batch Index  (sequential drive number in the scanned pool)")
            ax.set_ylabel(METRIC_YLABELS.get(metric, metric))
            # Merge legend entries (drive class + threshold lines)
            handles, labels = ax.get_legend_handles_labels()
            if handles:
                ax.legend(handles=handles, labels=labels, fontsize=8,
                          loc='upper right', framealpha=0.7)
            self.render_block(container, fig, key)

    # ==================================================================
    #  TAB: ANALYTICS
    # ==================================================================
    def render_analytics_tab(self):
        container = self.tab_analytics.scrollable_window
        for w in container.winfo_children():
            w.destroy()
        analyzer = TelemetryAnalyzer(self.current_df)

        # 1 — Correlation Heatmap
        fig1, ax1 = plt.subplots(figsize=(15, 8))
        corr_cols = ['Temperature', 'Available Spare', 'Endurance Used',
                     'Media Errors', 'Unsafe Shutdowns', 'Error Log Entries']
        sns.heatmap(self.current_df[corr_cols].corr(), annot=True, fmt=".2f",
                    cmap="RdBu_r", center=0, ax=ax1, linewidths=0.5)
        ax1.set_title("Master Telemetry Correlation Matrix (Pearson r)")
        self.render_block(container, fig1, "correlation")

        # 2 — Thermal KDE
        fig2, ax2 = plt.subplots(figsize=(15, 6))
        if self.current_df['Temperature'].nunique() > 1:
            sns.kdeplot(data=self.current_df, x="Temperature",
                        hue="Drive Class", fill=True, alpha=0.35, ax=ax2)
        else:
            ax2.axvline(self.current_df['Temperature'].iloc[0], color=ACCENT, lw=2)
            ax2.set_xlim(0, 100)
        ax2.axvline(70, color=ERROR,   ls="--", lw=2,   label="70\u00b0C Limit")
        ax2.axvline(55, color=WARNING, ls=":",  lw=1.5, label="55\u00b0C Caution")
        ax2.legend()
        ax2.set_xlabel("Temperature  (°C — 55°C caution, 70°C = JEDEC JESD219A upper limit)")
        ax2.set_ylabel("Drive Density  (relative proportion of pool at this temperature)")
        ax2.set_title("Pool Thermal Population Density (KDE)")
        self.render_block(container, fig2, "thermal_density")

        # 3 — WIF Efficiency Regression
        fig3, ax3 = plt.subplots(figsize=(15, 6))
        sns.scatterplot(data=self.current_df, x="Endurance Used", y="Data Units Written",
                        hue="Drive Class", s=100, ax=ax3)
        if (self.current_df['Endurance Used'].nunique() > 1
                and self.current_df['Data Units Written'].sum() > 0):
            try:
                z3 = np.polyfit(self.current_df['Endurance Used'],
                                self.current_df['Data Units Written'], 1)
                xs3 = np.linspace(self.current_df['Endurance Used'].min(),
                                  self.current_df['Endurance Used'].max(), 100)
                ax3.plot(xs3, np.poly1d(z3)(xs3), color=ERROR, ls="--", lw=2, label="Pool Trend")
                ax3.legend()
            except np.linalg.LinAlgError:
                pass
        ax3.set_xlabel("Endurance Used  (% — 0% = new,  100% = end of rated life)")
        ax3.set_ylabel("Data Units Written  (512-byte LBA units written by host)")
        ax3.set_title("WIF Efficiency Regression — Data Written vs. Endurance Consumed")
        self.render_block(container, fig3, "wif_index")

        # 4 — Anomaly Detection Scatter
        anomalies     = analyzer.detect_anomalies()
        anomaly_serials = set(anomalies['Serial'].tolist()) if not anomalies.empty else set()
        normal_mask   = ~self.current_df['Serial'].isin(anomaly_serials)
        anomaly_mask  =  self.current_df['Serial'].isin(anomaly_serials)
        fig4, ax4 = plt.subplots(figsize=(15, 7))
        ax4.scatter(self.current_df.loc[normal_mask, 'Temperature'],
                    self.current_df.loc[normal_mask, 'Endurance Used'],
                    c=ACCENT, s=60, alpha=0.55, label=f"Normal ({normal_mask.sum()})", zorder=1)
        if anomaly_mask.any():
            ax4.scatter(self.current_df.loc[anomaly_mask, 'Temperature'],
                        self.current_df.loc[anomaly_mask, 'Endurance Used'],
                        c=ERROR, s=180, alpha=0.9, marker='*', edgecolors='white', linewidths=0.5,
                        label=f"Anomaly \u2014 {anomaly_mask.sum()} drives flagged", zorder=2)
        ax4.axvline(75, color=WARNING, ls="--", lw=1.5, label="75°C Warning")
        ax4.axhline(90, color=WARNING, ls=":",  lw=1.5, label="90% Wear Warning")
        ax4.set_xlabel("Temperature  (°C — 55°C caution, 70°C = JEDEC JESD219A upper limit)")
        ax4.set_ylabel("Endurance Used  (% — 0% = new,  100% = end of rated life)")
        ax4.set_title("Multi-Sensor Anomaly Detection  (Z-Score > 3\u03c3 — Red Stars = Flagged Outliers)")
        ax4.legend()
        self.render_block(container, fig4, "anomaly_scatter")

        # 5 — EOL Prediction
        eol_df = analyzer.predict_eol()
        if not eol_df.empty:
            show_df  = eol_df.head(25)
            fig5_h   = max(5, len(show_df) * 0.45)
            fig5, ax5 = plt.subplots(figsize=(15, fig5_h))
            bar_colors = [ERROR if d < 90 else (WARNING if d < 365 else ACCENT)
                          for d in show_df['Days to EOL'].values]
            y_pos = range(len(show_df))
            ax5.barh(y_pos, show_df['Days to EOL'].values, color=bar_colors, edgecolor='none')
            ax5.set_yticks(list(y_pos))
            ax5.set_yticklabels(
                [f"{r['Serial']}  ({r['Drive Class']})" for _, r in show_df.iterrows()],
                fontsize=8
            )
            ax5.axvline(90,  color=ERROR,   ls="--", lw=2, label="90 days (Critical)")
            ax5.axvline(365, color=WARNING, ls="--", lw=2, label="1 Year (Plan Now)")
            ax5.set_xlabel("Estimated Days to End of Life  (Linear Extrapolation)")
            ax5.set_title("Predictive EOL Runway — Top 25 Most At-Risk Drives")
            ax5.legend()
            plt.tight_layout()
            self.render_block(container, fig5, "eol_prediction")

        # 6 — WAF Proxy
        waf_df = analyzer.calculate_waf()
        if not waf_df.empty and waf_df['WAF_Proxy'].sum() > 0:
            fig6, ax6 = plt.subplots(figsize=(15, 6))
            sns.boxplot(data=waf_df, x="Drive Class", y="WAF_Proxy", ax=ax6, color=ACCENT, width=0.5)
            ax6.axhline(2,  color=SUCCESS, ls="--", lw=1.5, label="WAF 2  (Optimal)")
            ax6.axhline(5,  color=WARNING, ls="--", lw=1.5, label="WAF 5  (Caution)")
            ax6.axhline(10, color=ERROR,   ls="--", lw=1.5, label="WAF 10 (Critical)")
            ax6.set_title("Write Amplification Factor (WAF) Proxy — By Drive Class  (Lower = Better)")
            ax6.set_xlabel("Drive Class  (NAND type and capacity family)")
            ax6.set_ylabel("WAF Proxy  (NAND writes ÷ host writes — 1 = ideal,  >5 = high wear)")
            ax6.legend()
            self.render_block(container, fig6, "waf_chart")

    # ==================================================================
    #  TAB: QC / SIGMA
    # ==================================================================
    def render_qc_tab(self):
        container = self.tab_qc.scrollable_window
        for w in container.winfo_children():
            w.destroy()
        analyzer = TelemetryAnalyzer(self.current_df)
        cpk_val  = analyzer.get_cpk('Temperature', 70)

        # I-Chart
        data_t  = self.current_df['Temperature']
        mean_t  = data_t.mean()
        sigma_t = data_t.std() if data_t.std() > 0 else 0.001  # avoid div-by-zero on uniform data
        fig_i, ax_i = plt.subplots(figsize=(15, 6))
        self._add_threshold_overlay(ax_i, "Temperature", orient='h', add_legend=False)
        # Color individual points by pass/fail
        xs = list(range(len(data_t)))
        if self.show_thresholds.get():
            pt_colors = [ERROR if t > 70 else (WARNING if t > 55 else SUCCESS) for t in data_t]
            ax_i.scatter(xs, data_t, c=pt_colors, s=50, zorder=4, edgecolors='white', linewidths=0.3)
            ax_i.plot(xs, data_t, color=ACCENT, alpha=0.4, zorder=3)
        else:
            sns.lineplot(x=xs, y=data_t, marker='o', ax=ax_i, color=ACCENT, zorder=3)
        ax_i.axhline(mean_t,              color='white', ls='--', lw=1.5, label=f"Mean {mean_t:.1f}\u00b0C")
        ax_i.axhline(mean_t + 3*sigma_t, color=ERROR,   ls=':',  lw=2,   label=f"+3\u03c3 = {mean_t+3*sigma_t:.1f}\u00b0C")
        ax_i.axhline(mean_t - 3*sigma_t, color=ERROR,   ls=':',  lw=2,   label=f"-3\u03c3 = {mean_t-3*sigma_t:.1f}\u00b0C")
        ax_i.set_title("Six Sigma Temperature Control Chart  (Shewhart I-Chart)"
                       + ("  [\U0001f6a6 PASS/FAIL COLORED]" if self.show_thresholds.get() else ""))
        ax_i.set_xlabel("Batch Index  (sequential drive number in the scanned pool)")
        ax_i.set_ylabel("Temperature  (°C — outside ±3σ lines = anomalous)")
        ax_i.legend(fontsize=8, framealpha=0.7)
        self.render_block(container, fig_i, "control_chart")

        # Cpk Histogram
        fig_cpk, ax_cpk = plt.subplots(figsize=(15, 6))
        if self.show_thresholds.get():
            # Shade zones on the x-axis (vertical spans for histogram)
            ax_cpk.axvspan(0,  55, alpha=0.07, color=SUCCESS, zorder=0, label="PASS Zone")
            ax_cpk.axvspan(55, 70, alpha=0.10, color=WARNING, zorder=0, label="Caution Zone")
            ax_cpk.axvspan(70, 130, alpha=0.18, color=ERROR,  zorder=0, label="FAIL Zone")
        sns.histplot(self.current_df['Temperature'], kde=True, ax=ax_cpk, color=ACCENT, bins=30, zorder=2)
        ax_cpk.axvline(70,     color=ERROR,   lw=3,   label="USL = 70\u00b0C  (JESD219A)")
        ax_cpk.axvline(mean_t, color='white', ls='--', lw=1.5, label=f"Mean = {mean_t:.1f}\u00b0C")
        cpk_color = SUCCESS if cpk_val >= 1.33 else (WARNING if cpk_val >= 1.0 else ERROR)
        ax_cpk.set_title(f"Process Capability (Cpk = {cpk_val:.2f}"
                         f"  \u2014  {'PASS' if cpk_val>=1.33 else ('MARGINAL' if cpk_val>=1.0 else 'FAIL')})"
                         f"  \u2014  Thermal Conformance to 70\u00b0C Envelope")
        ax_cpk.title.set_color(cpk_color)
        ax_cpk.set_xlabel("Temperature  (°C — 70°C red line = JEDEC upper limit)")
        ax_cpk.set_ylabel("Drive Count  (# of drives per temperature bin)")
        ax_cpk.legend(fontsize=8, framealpha=0.7)
        self.render_block(container, fig_cpk, "capability")

        # Pareto
        p_data  = analyzer.get_pareto_data()
        fig_p, ax_p = plt.subplots(figsize=(15, 6))
        sns.barplot(data=p_data, x="Mode", y="Count", hue="Mode",
                    palette="Reds_r", ax=ax_p, legend=False)
        ax_p.set_title("Failure Mode Pareto  (Fix Tallest Bar First)")
        ax_p.set_xlabel("Failure Mode  (sensor or condition that triggered the flag)")
        ax_p.set_ylabel("Drive Count  (# affected drives)")
        ax_p.tick_params(axis='x', labelrotation=20)
        for bar in ax_p.patches:
            h = bar.get_height()
            if h > 0:
                ax_p.text(bar.get_x() + bar.get_width() / 2, h + 0.3,
                          str(int(h)), ha='center', va='bottom', fontsize=10, color=FG_TEXT)
        self.render_block(container, fig_p, "pareto_2")

        # Spare Violin
        fig_v, ax_v = plt.subplots(figsize=(15, 6))
        sns.violinplot(data=self.current_df, x="Drive Class", y="Available Spare",
                       ax=ax_v, color=ACCENT, inner="box")
        ax_v.axhline(90, color=WARNING, ls="--", lw=1.5, label="90% Warning Threshold")
        ax_v.axhline(10, color=ERROR,   ls="--", lw=1.5, label="10% JEDEC EOL Threshold")
        ax_v.set_xlabel("Drive Class  (NAND type and capacity family)")
        ax_v.set_ylabel("Available Spare  (% — 100% = full,  10% = JEDEC end-of-life)")
        ax_v.set_title("Pool Spare Block Reservation Spread  (Violin)")
        ax_v.legend()
        self.render_block(container, fig_v, "spare_margin")

        # Firmware Maturity
        if self.current_df['Firmware'].nunique() >= 1:
            fig_fw, ax_fw = plt.subplots(figsize=(15, 6))
            try:
                fw = pd.crosstab(self.current_df['Firmware'],
                                 self.current_df['Overall Status'], normalize='index') * 100
                order      = [c for c in ['ERROR', 'WARNING', 'OK'] if c in fw.columns]
                color_map  = {'ERROR': ERROR, 'WARNING': WARNING, 'OK': SUCCESS}
                fw[order].plot(kind='bar', stacked=True, ax=ax_fw,
                               color=[color_map.get(c, FG_SUB) for c in order])
                ax_fw.set_title("Firmware Revision Yield Maturity  (% Status per FW Version)")
                ax_fw.set_xlabel("Firmware Revision  (SSD software version — newer ≠ better)")
                ax_fw.set_ylabel("% of Drives on This Revision  (green = OK,  yellow = WARNING,  red = ERROR)")
                ax_fw.tick_params(axis='x', labelrotation=30)
                ax_fw.legend(title="Status", loc='upper right')
                self.render_block(container, fig_fw, "fw_parity")
            except Exception as e:
                ax_fw.text(0.5, 0.5, f"Firmware chart unavailable:\n{e}",
                           ha='center', va='center', transform=ax_fw.transAxes,
                           color=FG_SUB, fontsize=11)
                self.render_block(container, fig_fw, "fw_parity")

    # ==================================================================
    #  TAB: DATASET HUB
    # ==================================================================
    def render_actions_hub(self):
        container = self.tab_actions.scrollable_window
        for w in container.winfo_children():
            w.destroy()
        hub = tk.Frame(container, bg=BG_MAIN, pady=40)
        hub.pack(fill="x", padx=60)

        tk.Label(hub, text="\U0001f6e0\ufe0f  MASTER DATASET HUB",
                 font=("Segoe UI", 22, "bold"), fg=ACCENT, bg=BG_MAIN).pack(anchor="w")
        tk.Label(hub, text="Export, analyze, and distribute all telemetry data from this batch. All exports include full metadata headers.",
                 font=("Segoe UI", 10), fg=FG_SUB, bg=BG_MAIN).pack(anchor="w", pady=(4, 20))

        # ---- File Processing Stats ----
        meta = self.processor.metadata
        analyzer = TelemetryAnalyzer(self.current_df)
        mtbf  = analyzer.estimate_mtbf()
        score = analyzer.calculate_fleet_score()
        n_err = len(self.current_df[self.current_df['Overall Status'] == 'ERROR'])
        n_anom = len(analyzer.detect_anomalies())

        sf = tk.Frame(hub, bg=BG_CARD, padx=40, pady=25,
                      highlightbackground=FG_SUB, highlightthickness=1)
        sf.pack(fill="x", pady=(0, 20))
        tk.Label(sf, text="BATCH PROCESSING STATISTICS", font=("Segoe UI", 11, "bold"),
                 fg=ACCENT, bg=BG_CARD).pack(anchor="w", pady=(0, 12))
        stat_row = tk.Frame(sf, bg=BG_CARD)
        stat_row.pack(fill="x")
        for k, v, c in [
            ("TOTAL FILES FOUND",   meta['total_files_found'],    FG_TEXT),
            ("PARSED SUCCESSFULLY", meta['parsed_successfully'],  SUCCESS),
            ("SKIPPED / CORRUPTED", meta['skipped_files'],        WARNING if meta['skipped_files'] > 0 else FG_TEXT),
            ("POOL SCORE",         f"{score:.1f} / 100",         SUCCESS if score > 85 else (WARNING if score > 70 else ERROR)),
            ("CRITICAL / RMA",      n_err,                         ERROR if n_err > 0 else SUCCESS),
            ("ANOMALIES FLAGGED",   n_anom,                        WARNING if n_anom > 0 else SUCCESS),
            ("EST. POOL MTBF",     f"{int(mtbf):,} hrs" if mtbf != float('inf') else "\u221e", FG_TEXT),
        ]:
            f = tk.Frame(stat_row, bg=BG_CARD)
            f.pack(side="left", expand=True)
            tk.Label(f, text=str(k), font=("Segoe UI", 8, "bold"), fg=FG_SUB, bg=BG_CARD).pack()
            tk.Label(f, text=str(v), font=("Segoe UI", 20, "bold"), fg=c, bg=BG_CARD).pack()

        # ---- Parse Errors ----
        if meta['errors']:
            ef = tk.Frame(hub, bg=BG_CARD, padx=30, pady=15,
                          highlightbackground=WARNING, highlightthickness=1)
            ef.pack(fill="x", pady=(0, 20))
            tk.Label(ef, text=f"\u26a0\ufe0f  {len(meta['errors'])} PARSE ERROR(S) — FILES SKIPPED",
                     font=("Segoe UI", 10, "bold"), fg=WARNING, bg=BG_CARD).pack(anchor="w")
            for err in meta['errors'][:8]:
                SelectableLabel(ef, text=f"  \u2022 {err}", font=("Consolas", 9), fg=FG_SUB,
                         bg=BG_CARD).pack(anchor="w", fill="x")
            if len(meta['errors']) > 8:
                tk.Label(ef, text=f"  ... and {len(meta['errors'])-8} more. Export Stats Summary CSV for full list.",
                         font=("Segoe UI", 9, "italic"), fg=FG_SUB, bg=BG_CARD).pack(anchor="w")

        # ---- Aggregation Table ----
        tk.Label(hub, text="POOL AGGREGATION SUMMARY",
                 font=("Segoe UI", 14, "bold"), fg=FG_TEXT, bg=BG_MAIN).pack(anchor="w", pady=(10, 6))
        tk.Label(hub, text="Group-level statistics across all primary telemetry metrics. Click column headers to sort.",
                 font=("Segoe UI", 9), fg=FG_SUB, bg=BG_MAIN).pack(anchor="w", pady=(0, 8))
        self._render_aggregation_table(hub, analyzer.get_aggregation_summary())

        # ---- Export Center ----
        tk.Label(hub, text="EXPORT CENTER",
                 font=("Segoe UI", 14, "bold"), fg=FG_TEXT, bg=BG_MAIN).pack(anchor="w", pady=(30, 8))
        btn_frame = tk.Frame(hub, bg=BG_MAIN)
        btn_frame.pack(fill="x")

        exports = [
            ("\U0001f4e5 Full Pool CSV",        ACCENT,   self._export_fleet_csv),
            ("\u26a0\ufe0f  RMA Priority List CSV", ERROR, self._export_rma_csv),
            ("\U0001f4c4 Multi-Sheet Excel",     SUCCESS,  self._export_multisheet_excel),
            ("\U0001f9ea Anomaly Report CSV",    WARNING,  self._export_anomaly_report),
            ("\U0001f4ca Pool Stats Summary CSV", "#8b5cf6", self._export_stats_summary),
            ("\U0001f4e6 Structured JSON Export", "#06b6d4", self._export_json),
        ]
        row = None
        for i, (label, color, cmd) in enumerate(exports):
            if i % 3 == 0:
                row = tk.Frame(btn_frame, bg=BG_MAIN)
                row.pack(fill="x", pady=5)
            tk.Button(row, text=label, bg=color, fg="white",
                      font=("Segoe UI", 10, "bold"), padx=25, pady=12, relief="flat",
                      command=cmd, cursor="hand2").pack(side="left", padx=8)

        # ---- Failure Risk Analysis ----
        self._render_risk_analysis_panel(hub, analyzer)

        # ---- Engineering Reference ----
        tk.Label(hub, text="\U0001f4da  ENGINEERING REFERENCE STANDARDS",
                 font=("Segoe UI", 14, "bold"), fg=FG_TEXT, bg=BG_MAIN).pack(anchor="w", pady=(40, 10))
        refs = [
            ("JEDEC JESD218B",
             "SSD Endurance Workload Definition. Defines TBW (Total Bytes Written) and DWPD (Drive Writes Per Day) metrics used to set "
             "the endurance % threshold.  The 10% spare threshold and end-of-life state described in this app map directly to JESD218B Section 4.3."),
            ("NVMe Base Specification 1.4c — SMART/Health Log (Section 5.14.1)",
             "Defines all telemetry fields read by this application: Critical Warning, Temperature, Available Spare, Available Spare Threshold, "
             "Percentage Used, Data Units Read/Written, Power On Hours, Unsafe Shutdowns, Media and Data Integrity Errors, Error Log Entries."),
            ("JEDEC JESD219A — Solid State Drive (SSD) Thermal Test Method",
             "Defines operating temperature test conditions for enterprise SSDs. The 70°C USL used in the Cpk and I-Chart analyses is derived from "
             "JESD219A Table 3 (Enterprise Class A, max sustained operating temperature)."),
            ("Six Sigma DMAIC — AIAG SPC Manual 2nd Edition",
             "This console implements the Measure and Analyze stages of the DMAIC workflow. "
             "The I-Chart, Cpk, and Pareto charts are formal SPC tools per the AIAG manual. "
             "Combine with the Define and Control stages using external FMEA documentation for full compliance."),
            ("Bathtub Reliability Curve (Weibull Distribution)",
             "Reliability engineering framework for lifetime modeling. Early-life failures (infant mortality) have decreasing hazard rate (Weibull β < 1). "
             "Wear-out failures have increasing hazard rate (β > 1). Drives flagged as anomalies in this app are candidates for infant mortality classification."),
        ]
        for title, body in refs:
            rf = tk.Frame(hub, bg=BG_CARD, padx=30, pady=18, borderwidth=1, relief="solid")
            rf.pack(fill="x", pady=6)
            tk.Label(rf, text=title, font=("Segoe UI", 11, "bold"), fg=SUCCESS, bg=BG_CARD).pack(anchor="w")
            SelectableLabel(rf, text=body,  font=("Segoe UI", 9),  fg=FG_SUB, bg=BG_CARD).pack(anchor="w", pady=(4, 0), fill="x")

    # ------------------------------------------------------------------
    #  FAILURE RISK ANALYSIS PANEL
    # ------------------------------------------------------------------
    def _render_risk_analysis_panel(self, parent, analyzer: TelemetryAnalyzer):
        """
        Generates and renders the multi-level failure risk analysis section,
        grouped by category with clear section dividers.
        """
        RISK_COLORS = {
            "CRITICAL": ERROR,
            "HIGH":     "#f97316",
            "WARNING":  WARNING,
            "INFO":     ACCENT,
            "PASS":     SUCCESS,
        }
        RISK_ICONS = {
            "CRITICAL": "\U0001f534",
            "HIGH":     "\U0001f7e0",
            "WARNING":  "\U0001f7e1",
            "INFO":     "\U0001f535",
            "PASS":     "\U0001f7e2",
        }
        REC_BG = {
            "CRITICAL": "#200000",
            "HIGH":     "#1a0e00",
            "WARNING":  "#1a1200",
            "INFO":     "#001428",
            "PASS":     "#0a1f14",
        }
        # Canonical category display order and labels
        CATEGORY_ORDER = [
            "INTERFACE / SIGNAL INTEGRITY",
            "PCB / HARDWARE — POWER DELIVERY",
            "PCB / HARDWARE — BGA RELIABILITY",
            "NAND COMPONENT — DIE HEALTH",
            "NAND COMPONENT — SPARE POOL EXHAUSTION",
            "NAND COMPONENT — WRITE AMPLIFICATION",
            "FIRMWARE / CONTROLLER",
            "MANUFACTURING / BATCH QUALITY",
            "PROCESS / QUALIFICATION SCOPE",
            "ALL SYSTEMS",
        ]
        CATEGORY_ICONS = {
            "INTERFACE / SIGNAL INTEGRITY":       "\U0001f50c",
            "PCB / HARDWARE — POWER DELIVERY":    "\u26a1",
            "PCB / HARDWARE — BGA RELIABILITY":   "\U0001f9f2",
            "NAND COMPONENT — DIE HEALTH":        "\U0001f9e0",
            "NAND COMPONENT — SPARE POOL EXHAUSTION": "\U0001f4c9",
            "NAND COMPONENT — WRITE AMPLIFICATION":   "\u270f\ufe0f",
            "FIRMWARE / CONTROLLER":              "\U0001f4bb",
            "MANUFACTURING / BATCH QUALITY":      "\U0001f3ed",
            "PROCESS / QUALIFICATION SCOPE":      "\U0001f4cb",
            "ALL SYSTEMS":                        "\U0001f7e2",
        }

        # Section header
        sep = tk.Frame(parent, bg="#334155", height=2)
        sep.pack(fill="x", pady=(30, 0))
        tk.Label(parent,
                 text="\U0001f9ec  FAILURE RISK ANALYSIS",
                 font=("Segoe UI", 16, "bold"), fg=FG_TEXT, bg=BG_MAIN).pack(anchor="w", pady=(12, 2))
        SelectableLabel(parent,
                 text="Telemetry-derived risk findings grouped by failure domain. "
                      "Each finding includes root-cause detail, actionable recommendation, and standards reference. "
                      "Address CRITICAL and HIGH findings before deployment or RMA filing.",
                 font=("Segoe UI", 9), fg=FG_SUB, bg=BG_MAIN).pack(
                 anchor="w", pady=(0, 16), fill="x")

        findings = analyzer.generate_risk_report()
        if not findings:
            tk.Label(parent, text="No risk findings generated.", font=("Segoe UI", 11),
                     fg=FG_SUB, bg=BG_MAIN).pack(anchor="w")
            return

        # Group findings by category, preserving canonical order
        grouped: dict[str, list] = {}
        for f in findings:
            cat = f.get("category", "OTHER")
            grouped.setdefault(cat, []).append(f)

        # Render in canonical order, then any remaining categories not in order
        rendered_cats = set()
        for cat in CATEGORY_ORDER:
            if cat in grouped:
                self._render_risk_category(parent, cat, grouped[cat],
                                           CATEGORY_ICONS, RISK_COLORS, RISK_ICONS, REC_BG)
                rendered_cats.add(cat)
        for cat, flist in grouped.items():
            if cat not in rendered_cats:
                self._render_risk_category(parent, cat, flist,
                                           CATEGORY_ICONS, RISK_COLORS, RISK_ICONS, REC_BG)

    def _render_risk_category(self, parent, category, findings,
                               cat_icons, risk_colors, risk_icons, rec_bg):
        """Renders one category block: a labeled divider followed by finding cards."""
        cat_icon = cat_icons.get(category, "\u25a0")

        # Category divider bar
        div = tk.Frame(parent, bg="#1e293b", pady=10, padx=20,
                       highlightbackground="#334155", highlightthickness=1)
        div.pack(fill="x", pady=(20, 4))
        tk.Label(div, text=f"{cat_icon}  {category}",
                 font=("Segoe UI", 12, "bold"), fg="#94a3b8", bg="#1e293b").pack(anchor="w")

        # Individual finding cards
        for f in findings:
            risk    = f.get("risk",             "INFO")
            color   = risk_colors.get(risk,     ACCENT)
            icon    = risk_icons.get(risk,      "\u25cf")
            finding = f.get("finding",          "")
            detail  = f.get("detail",           "")
            rec     = f.get("recommendation",   "")
            spec    = f.get("spec_ref",         "")
            rbg     = rec_bg.get(risk,          "#111827")

            card = tk.Frame(parent, bg=BG_CARD, padx=28, pady=18,
                            highlightbackground=color, highlightthickness=2)
            card.pack(fill="x", pady=4, padx=20)

            # Risk badge
            badge_row = tk.Frame(card, bg=BG_CARD)
            badge_row.pack(fill="x", anchor="w")
            tk.Label(badge_row, text=f"{icon}  {risk}",
                     font=("Segoe UI", 10, "bold"), fg=color, bg=BG_CARD).pack(side="left")

            # Finding headline
            SelectableLabel(card, text=finding, font=("Segoe UI", 11, "bold"),
                     fg=FG_TEXT, bg=BG_CARD).pack(
                     anchor="w", pady=(8, 4), fill="x")

            # Technical detail
            if detail:
                SelectableLabel(card, text=detail, font=("Segoe UI", 9),
                         fg=FG_SUB, bg=BG_CARD).pack(
                         anchor="w", padx=(8, 0), pady=(0, 6), fill="x")

            # Recommendation block
            if rec:
                rf = tk.Frame(card, bg=rbg)
                rf.pack(fill="x", pady=(6, 0))
                tk.Label(rf, text="\u25b6  RECOMMENDATION",
                         font=("Segoe UI", 9, "bold"), fg=color,
                         bg=rbg, padx=12, pady=5).pack(anchor="w")
                SelectableLabel(rf, text=rec, font=("Segoe UI", 9),
                         fg=FG_TEXT, bg=rbg,
                         padx=22, pady=8).pack(anchor="w", fill="x")

            # Spec reference footer
            if spec:
                SelectableLabel(card, text=f"\U0001f4cb  {spec}",
                         font=("Consolas", 8), fg="#475569",
                         bg=BG_CARD).pack(anchor="w", pady=(8, 0), fill="x")

    # ------------------------------------------------------------------
    #  AGGREGATION TABLE WIDGET
    # ------------------------------------------------------------------
    def _render_aggregation_table(self, parent, agg_df):
        frame = tk.Frame(parent, bg=BG_CARD)
        frame.pack(fill="x", pady=(0, 20))
        cols = list(agg_df.columns)
        tree = ttk.Treeview(frame, columns=cols, show='headings', height=min(len(agg_df) + 1, 10))
        col_widths = {'Group': 160, 'N': 55, 'ERROR #': 70, 'WARN #': 70}
        for c in cols:
            w = col_widths.get(c, 95)
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="center")
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(xscrollcommand=hsb.set)
        for _, row in agg_df.iterrows():
            vals  = [row[c] for c in cols]
            label = str(row['Group'])
            tag   = 'fleet_total' if label == 'POOL TOTAL' else ''
            tree.insert('', 'end', values=vals, tags=(tag,))
        tree.tag_configure('fleet_total', background='#1d3461', foreground='white')
        tree.pack(fill="x")
        hsb.pack(fill="x")

    # ------------------------------------------------------------------
    #  PASS/FAIL THRESHOLD OVERLAY
    # ------------------------------------------------------------------
    def _toggle_thresholds(self):
        """Re-renders all chart tabs when the Pass/Fail toggle changes."""
        if self.current_df is not None and not self.current_df.empty:
            plt.close('all')
            self.rendered_tabs.clear()
            self._render_active_tab()

    def _add_threshold_overlay(self, ax, metric, orient='h', add_legend=True):
        """
        Adds enterprise pass/fail threshold zones and lines to an axis.
        orient='h'  -> horizontal bands (for y-axis metrics)
        orient='v'  -> vertical bands  (for x-axis metrics on scatter)
        Only draws anything when self.show_thresholds is True.
        """
        if not self.show_thresholds.get():
            return
        cfg = ENTERPRISE_THRESHOLDS.get(metric)
        if not cfg:
            return
        legend_handles = []
        if orient == 'h':
            for lo, hi, color, alpha, label in cfg.get("spans", []):
                ax.axhspan(lo, hi, alpha=alpha, color=color, zorder=0)
                legend_handles.append(mpatches.Patch(color=color, alpha=alpha + 0.15, label=label))
            for val, color, ls, label in cfg.get("lines", []):
                ax.axhline(val, color=color, ls=ls, lw=1.8, alpha=0.90, zorder=1, label=label)
        else:  # vertical
            for lo, hi, color, alpha, label in cfg.get("spans", []):
                ax.axvspan(lo, hi, alpha=alpha, color=color, zorder=0)
                legend_handles.append(mpatches.Patch(color=color, alpha=alpha + 0.15, label=label))
            for val, color, ls, label in cfg.get("lines", []):
                ax.axvline(val, color=color, ls=ls, lw=1.8, alpha=0.90, zorder=1, label=label)
        if add_legend and legend_handles:
            src_text = cfg.get("source", "")
            if src_text:
                ax.annotate(f"Thresholds: {src_text}", xy=(0.01, 0.01),
                            xycoords='axes fraction', fontsize=7, color=FG_SUB,
                            alpha=0.8, va='bottom')

    # ------------------------------------------------------------------
    #  GAUGE FIGURE
    # ------------------------------------------------------------------
    def _make_gauge_fig(self, score, color):
        fig, axes = plt.subplots(1, 2, figsize=(15, 4),
                                  gridspec_kw={'width_ratios': [3, 1]})
        fig.patch.set_facecolor(BG_CARD)

        ax = axes[0]
        ax.set_facecolor(BG_CARD)
        ax.barh(0, 100,   height=0.45, color='#1e3a5f', left=0,     zorder=1)
        ax.barh(0, score, height=0.45, color=color,     left=0,     zorder=2)
        for x, label, c in [(70, 'RISK', ERROR), (85, 'NOMINAL', WARNING)]:
            ax.axvline(x, color=c, ls='--', alpha=0.8, lw=2, zorder=3)
            ax.text(x, 0.28, label, ha='center', fontsize=9, color=c, fontweight='bold')
        for xs, xe, lbl, c in [(0, 70, 'CRITICAL ZONE', ERROR), (70, 85, 'CAUTION', WARNING), (85, 100, 'OPTIMAL', SUCCESS)]:
            ax.axvspan(xs, xe, alpha=0.06, color=c, zorder=0)
            ax.text((xs + xe) / 2, -0.32, lbl, ha='center', fontsize=8, color=c)
        label_x = max(score / 2, 5) if score > 10 else score + 3
        ax.text(label_x, 0, f"{score:.1f}", ha='center', va='center',
                fontsize=22, fontweight='bold', color='white', zorder=4)
        ax.set_xlim(0, 100)
        ax.set_ylim(-0.5, 0.55)
        ax.set_yticks([])
        ax.set_xlabel("Pool Mission-Readiness Score  (0 = Full Failure  /  100 = Perfect)",
                      color=FG_TEXT, fontsize=10)
        ax.set_title("POOL MISSION-READINESS GAUGE", color=FG_TEXT, fontsize=13, fontweight='bold', pad=14)
        ax.tick_params(colors=FG_SUB)

        ax2 = axes[1]
        ax2.set_facecolor(BG_CARD)
        ax2.axis('off')
        label = "OPTIMAL" if score > 85 else ("CAUTION" if score > 70 else "CRITICAL")
        ax2.text(0.5, 0.65, f"{score:.1f}", ha='center', va='center', fontsize=52,
                 fontweight='bold', color=color, transform=ax2.transAxes)
        ax2.text(0.5, 0.32, "/ 100", ha='center', va='center', fontsize=16,
                 color=FG_SUB, transform=ax2.transAxes)
        ax2.text(0.5, 0.12, label, ha='center', va='center', fontsize=13,
                 fontweight='bold', color=color, transform=ax2.transAxes)
        plt.tight_layout()
        return fig

    # ==================================================================
    #  EXPORT HELPERS
    # ==================================================================
    def _save_dialog(self, default_name, ext, label):
        return filedialog.asksaveasfilename(
            defaultextension=f".{ext}",
            filetypes=[(f"{label} files", f"*.{ext}"), ("All files", "*.*")],
            initialfile=default_name,
            title=f"Save {label}",
        )

    def _export_fleet_csv(self):
        path = self._save_dialog("triageio_fleet_export.csv", "csv", "CSV")
        if not path:
            return
        try:
            sanitize_df(self.current_df).to_csv(path, index=False)
            messagebox.showinfo("Export Complete", f"Full pool CSV saved:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    def _export_rma_csv(self):
        path = self._save_dialog("triageio_rma_priority_list.csv", "csv", "CSV")
        if not path:
            return
        try:
            rma = self.current_df[self.current_df['Overall Status'] == 'ERROR']
            sanitize_df(rma).to_csv(path, index=False)
            messagebox.showinfo("Export Complete",
                                f"RMA Priority List saved ({len(rma)} drives):\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    def _export_anomaly_report(self):
        path = self._save_dialog("triageio_anomaly_report.csv", "csv", "CSV")
        if not path:
            return
        try:
            analyzer = TelemetryAnalyzer(self.current_df)
            anom = analyzer.detect_anomalies()
            sanitize_df(anom).to_csv(path, index=False)
            messagebox.showinfo("Export Complete",
                                f"Anomaly Report saved ({len(anom)} flagged drives):\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    def _export_stats_summary(self):
        path = self._save_dialog("triageio_fleet_stats_summary.csv", "csv", "CSV")
        if not path:
            return
        try:
            analyzer = TelemetryAnalyzer(self.current_df)
            agg = analyzer.get_aggregation_summary()
            sanitize_df(agg).to_csv(path, index=False)
            messagebox.showinfo("Export Complete", f"Pool Stats Summary saved:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    def _export_json(self):
        path = self._save_dialog("triageio_fleet_export.json", "json", "JSON")
        if not path:
            return
        try:
            analyzer  = TelemetryAnalyzer(self.current_df)
            meta      = self.processor.metadata
            score     = analyzer.calculate_fleet_score()
            mtbf      = analyzer.estimate_mtbf()
            agg_df    = analyzer.get_aggregation_summary()
            percs     = analyzer.get_fleet_percentiles()
            eol_df    = analyzer.predict_eol()
            anom_df   = analyzer.detect_anomalies()

            payload = {
                "metadata": {
                    "generated_utc":      datetime.utcnow().isoformat(),
                    "source_folder":      self.results_dir.get(),
                    "total_files_found":  meta['total_files_found'],
                    "parsed_successfully":meta['parsed_successfully'],
                    "skipped_files":      meta['skipped_files'],
                    "fleet_score":        score,
                    "mtbf_hours":         int(mtbf) if mtbf != float('inf') else None,
                    "rma_count":          int(len(self.current_df[self.current_df['Overall Status'] == 'ERROR'])),
                    "warning_count":      int(len(self.current_df[self.current_df['Overall Status'] == 'WARNING'])),
                    "anomaly_count":      len(anom_df),
                },
                "percentiles":   percs,
                "aggregations":  agg_df.to_dict(orient='records'),
                "eol_predictions": eol_df.to_dict(orient='records') if not eol_df.empty else [],
                "anomalies":     anom_df[['Serial', 'Model', 'Drive Class', 'Anomaly_Score', 'Anomaly_Flags']].to_dict(orient='records') if not anom_df.empty else [],
                "pool":         self.current_df.to_dict(orient='records'),
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2, default=str)
            messagebox.showinfo("Export Complete", f"Structured JSON saved:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    def _export_multisheet_excel(self):
        path = self._save_dialog("triageio_full_analysis.xlsx", "xlsx", "Excel")
        if not path:
            return
        try:
            analyzer = TelemetryAnalyzer(self.current_df)
            agg_df   = analyzer.get_aggregation_summary()
            anom_df  = analyzer.detect_anomalies()
            eol_df   = analyzer.predict_eol()
            rma_df   = self.current_df[self.current_df['Overall Status'] == 'ERROR'].copy()
            perc_raw = analyzer.get_fleet_percentiles()
            perc_rows = [{"Metric": col, **vals} for col, vals in perc_raw.items()]
            perc_df  = pd.DataFrame(perc_rows)

            with pd.ExcelWriter(path, engine='openpyxl') as writer:
                sanitize_df(self.current_df).to_excel(writer, sheet_name="Pool Data",       index=False)
                sanitize_df(rma_df).to_excel(writer, sheet_name="RMA Priority List", index=False)
                sanitize_df(agg_df).to_excel(writer, sheet_name="Aggregations",      index=False)
                if not anom_df.empty:
                    sanitize_df(anom_df).to_excel(writer, sheet_name="Anomaly Report",    index=False)
                if not eol_df.empty:
                    sanitize_df(eol_df).to_excel(writer, sheet_name="EOL Predictions",   index=False)
                sanitize_df(perc_df).to_excel(writer, sheet_name="Percentiles",       index=False)

            messagebox.showinfo("Export Complete",
                                f"Multi-Sheet Excel saved (6 sheets):\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))


def launch_gui():
    """Creates the Tk root, applies theme, and starts the GUI event loop."""
    root = tk.Tk()
    root.title("TriageIO - Pool Telemetry Manager")
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    TriageIODesktop(root)
    root.mainloop()

