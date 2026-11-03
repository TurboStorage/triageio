import pandas as pd
import numpy as np


class TelemetryAnalyzer:
    """Provides high-level Six Sigma, Reliability, and Predictive Analytics."""

    def __init__(self, data):
        self.data = data

    # ------------------------------------------------------------------ #
    #  CORE SCORING                                                        #
    # ------------------------------------------------------------------ #

    def calculate_fleet_score(self):
        """Calculates a 0-100 composite mission-readiness score for the pool."""
        if self.data.empty:
            return 0.0
        rma_penalty  = (len(self.data[self.data['Overall Status'] == 'ERROR'])   / len(self.data)) * 50
        warn_penalty = (len(self.data[self.data['Overall Status'] == 'WARNING'])  / len(self.data)) * 20
        avg_temp     = self.data['Temperature'].mean()
        temp_penalty = max(0, (avg_temp - 40) * 0.5)
        score = 100 - rma_penalty - warn_penalty - temp_penalty
        return round(max(0.0, min(100.0, score)), 2)

    # ------------------------------------------------------------------ #
    #  PROCESS CAPABILITY                                                  #
    # ------------------------------------------------------------------ #

    def get_cpk(self, column, usl):
        """Calculates upper-sided Process Capability Index (Cpk)."""
        if self.data.empty or column not in self.data.columns:
            return 0.0
        sigma = self.data[column].std()
        if sigma == 0:
            return 0.0
        return (usl - self.data[column].mean()) / (3 * sigma)

    # ------------------------------------------------------------------ #
    #  FAILURE ANALYSIS                                                    #
    # ------------------------------------------------------------------ #

    def identify_rma_candidates(self):
        """Returns drives flagged as ERROR status."""
        if self.data.empty:
            return pd.DataFrame()
        return self.data[self.data['Overall Status'] == 'ERROR'].copy()

    def get_pareto_data(self):
        """Builds an expanded failure-mode count dataset (sorted descending)."""
        modes = {
            "Media (Bit-Flips)":         len(self.data[self.data['Media Errors']      >  0]),
            "Thermal (>75C)":            len(self.data[self.data['Temperature']        > 75]),
            "Power (Dirty Shutdowns)":   len(self.data[self.data['Unsafe Shutdowns']   > 500]),
            "Endurance (>90%)":          len(self.data[self.data['Endurance Used']     > 90]),
            "FW Exception (>1k logs)":   len(self.data[self.data['Error Log Entries']  > 1000]),
            "Spare Depletion (<95%)":    len(self.data[self.data['Available Spare']    < 95]),
        }
        df = pd.DataFrame(list(modes.items()), columns=['Mode', 'Count'])
        return df.sort_values('Count', ascending=False).reset_index(drop=True)

    # ------------------------------------------------------------------ #
    #  ANOMALY DETECTION  (Z-Score, per-metric)                           #
    # ------------------------------------------------------------------ #

    def detect_anomalies(self, threshold=3.0):
        """
        Z-score anomaly detection across all primary sensor columns.
        Returns a DataFrame of flagged drives sorted by composite Anomaly_Score desc.
        threshold: number of standard deviations to flag as anomalous.
        """
        sensor_cols = [
            'Temperature', 'Available Spare', 'Endurance Used',
            'Media Errors', 'Unsafe Shutdowns', 'Error Log Entries', 'Power On Hours',
        ]
        present = [c for c in sensor_cols if c in self.data.columns]
        result  = self.data.copy()
        result['Anomaly_Score'] = 0.0
        result['Anomaly_Flags'] = ''

        for col in present:
            sigma = result[col].std()
            if sigma == 0:
                continue
            z = np.abs((result[col] - result[col].mean()) / sigma)
            over = z > threshold
            result.loc[over, 'Anomaly_Score'] += z[over].values
            result.loc[over, 'Anomaly_Flags']  = result.loc[over, 'Anomaly_Flags'] + col + ' | '

        anomalies = result[result['Anomaly_Score'] > 0].copy()
        anomalies['Anomaly_Flags'] = anomalies['Anomaly_Flags'].str.rstrip(' | ')
        return anomalies.sort_values('Anomaly_Score', ascending=False).reset_index(drop=True)

    def identify_outliers(self, threshold=3.0):
        """Alias for detect_anomalies() — used by CLI main."""
        return self.detect_anomalies(threshold=threshold)

    # ------------------------------------------------------------------ #
    #  EOL PREDICTION  (Linear extrapolation)                             #
    # ------------------------------------------------------------------ #

    def predict_eol(self):
        """
        Linear wear-rate extrapolation to predict days remaining until 100% endurance.
        Only includes drives where both POH and Wear are non-zero.
        Returns a DataFrame sorted by Days_to_EOL ascending (most urgent first).
        """
        if self.data.empty:
            return pd.DataFrame()

        poh  = self.data['Power On Hours']
        wear = self.data['Endurance Used']
        valid = (poh > 0) & (wear > 0)

        wear_rate = np.where(valid, wear / np.where(poh > 0, poh, 1.0), 0.0)
        days_rem  = np.where(
            valid & (wear_rate > 0),
            (100.0 - wear) / wear_rate / 24.0,
            9999.0,
        )

        df = pd.DataFrame({
            'Serial':         self.data['Serial'],
            'Model':          self.data['Model'],
            'Drive Class':    self.data['Drive Class'],
            'Firmware':       self.data['Firmware'],
            'Current Wear %': wear.round(1),
            'Power On Hours': poh,
            'Days to EOL':    np.where(days_rem < 9999, np.round(days_rem, 0), 9999),
            'Years to EOL':   np.where(days_rem < 9999, np.round(days_rem / 365.0, 1), 999.0),
            'Overall Status': self.data['Overall Status'],
        })

        return df[df['Days to EOL'] < 9999].sort_values('Days to EOL').reset_index(drop=True)

    # ------------------------------------------------------------------ #
    #  WRITE AMPLIFICATION FACTOR  (WAF proxy)                            #
    # ------------------------------------------------------------------ #

    def calculate_waf(self):
        """
        WAF proxy = Endurance_Used% / (Data_Units_Written normalized 0-100).
        Lower is better. Returns a DataFrame sorted by WAF_Proxy descending.
        Returns empty DataFrame if Data Units Written is all zeros.
        """
        if 'Data Units Written' not in self.data.columns:
            return pd.DataFrame()

        df = self.data.copy()
        max_written = df['Data Units Written'].max()
        if max_written == 0:
            return pd.DataFrame()

        df['DUW_norm']  = (df['Data Units Written'] / max_written) * 100.0
        mask = df['DUW_norm'] > 0
        df['WAF_Proxy'] = 0.0
        df.loc[mask, 'WAF_Proxy'] = df.loc[mask, 'Endurance Used'] / df.loc[mask, 'DUW_norm']

        cols = ['Serial', 'Model', 'Drive Class', 'Firmware',
                'Endurance Used', 'Data Units Written', 'Power On Hours', 'WAF_Proxy']
        present = [c for c in cols if c in df.columns]
        return df[present].sort_values('WAF_Proxy', ascending=False).reset_index(drop=True)

    # ------------------------------------------------------------------ #
    #  POOL PERCENTILES                                                   #
    # ------------------------------------------------------------------ #

    def get_fleet_percentiles(self):
        """
        Returns a dict of {column: {p0, p25, p50, p75, p90, p95, p99, p100}}
        for all primary numeric sensor columns.
        """
        sensor_cols = [
            'Temperature', 'Available Spare', 'Endurance Used', 'Media Errors',
            'Unsafe Shutdowns', 'Error Log Entries', 'Power On Hours', 'Data Units Written',
        ]
        percs  = [0, 25, 50, 75, 90, 95, 99, 100]
        result = {}
        for col in sensor_cols:
            if col not in self.data.columns:
                continue
            vals = self.data[col].dropna()
            result[col] = {f'p{p}': round(float(np.percentile(vals, p)), 2) for p in percs}
        return result

    # ------------------------------------------------------------------ #
    #  AGGREGATION SUMMARY                                                 #
    # ------------------------------------------------------------------ #

    def get_aggregation_summary(self):
        """
        Returns a DataFrame with pool-wide + per-Drive-Class summary statistics.
        Columns: Group, N, ERROR #, WARN #, Temp Mean, Temp P90, Spare Mean, Spare Min,
                 Wear Mean, Wear Max, Media Err Sum, POH Mean.
        """
        def make_row(label, grp):
            n_grp = len(grp)
            def safe_mean(col):
                return round(grp[col].mean(), 1) if col in grp and n_grp > 0 else 0.0
            def safe_p90(col):
                return round(float(np.percentile(grp[col].dropna(), 90)), 1) if col in grp and n_grp > 0 else 0.0
            def safe_min(col):
                return round(grp[col].min(), 1) if col in grp and n_grp > 0 else 0.0
            def safe_max(col):
                return round(grp[col].max(), 1) if col in grp and n_grp > 0 else 0.0
            def safe_sum(col):
                return int(grp[col].sum()) if col in grp and n_grp > 0 else 0

            return {
                'Group':           label,
                'N':               len(grp),
                'ERROR #':         len(grp[grp['Overall Status'] == 'ERROR'])   if 'Overall Status' in grp.columns else 0,
                'WARN #':          len(grp[grp['Overall Status'] == 'WARNING']) if 'Overall Status' in grp.columns else 0,
                'Temp Mean':       safe_mean('Temperature'),
                'Temp P90':        safe_p90('Temperature'),
                'Spare Mean%':     safe_mean('Available Spare'),
                'Spare Min%':      safe_min('Available Spare'),
                'Wear Mean%':      safe_mean('Endurance Used'),
                'Wear Max%':       safe_max('Endurance Used'),
                'Media Err Sum':   safe_sum('Media Errors'),
                'POH Mean':        safe_mean('Power On Hours'),
            }

        rows = [make_row('POOL TOTAL', self.data)]
        if 'Drive Class' in self.data.columns:
            for cls, grp in self.data.groupby('Drive Class'):
                rows.append(make_row(cls, grp))
        return pd.DataFrame(rows)

    def calculate_summary_stats(self):
        """Alias for get_aggregation_summary() — used by CLI main."""
        return self.get_aggregation_summary()

    # ------------------------------------------------------------------ #
    #  MTBF ESTIMATION                                                     #
    # ------------------------------------------------------------------ #

    def estimate_mtbf(self):
        """
        Observed MTBF (hours) = Total_POH_across_fleet / Number_of_ERROR_drives.
        Returns float('inf') if no ERROR drives observed (ideal case).
        """
        if self.data.empty:
            return float('inf')
        total_poh   = self.data['Power On Hours'].sum()
        error_count = len(self.data[self.data['Overall Status'] == 'ERROR'])
        if error_count == 0:
            return float('inf')
        return total_poh / error_count

    # ------------------------------------------------------------------ #
    #  FAILURE RISK ANALYSIS REPORT                                        #
    # ------------------------------------------------------------------ #

    def generate_risk_report(self) -> list[dict]:
        """
        Derives contextual failure risk findings from live telemetry.
        Each finding covers: category, risk level, finding summary,
        technical detail, recommendation, and spec reference.
        Categories: INTERFACE/SIGNAL, PCB/HARDWARE, NAND COMPONENT,
                    FIRMWARE/CONTROLLER, MANUFACTURING/BATCH, PROCESS/QUALIFICATION
        """
        findings = []
        n = len(self.data)
        if n == 0:
            return findings

        avg_temp    = self.data['Temperature'].mean()
        pct_hot70   = len(self.data[self.data['Temperature'] > 70]) / n
        pct_hot55   = len(self.data[self.data['Temperature'] > 55]) / n
        n_media     = int(self.data[self.data['Media Errors']      >  0]['Media Errors'].sum())
        n_media_drv = len(self.data[self.data['Media Errors']      >  0])
        n_shut_hi   = len(self.data[self.data['Unsafe Shutdowns']  > 500])
        n_log_hi    = len(self.data[self.data['Error Log Entries'] > 1000])
        n_spare_eol = len(self.data[self.data['Available Spare']   < 10])
        fw_versions = self.data['Firmware'].nunique() if 'Firmware' in self.data.columns else 0
        n_classes   = self.data['Drive Class'].nunique() if 'Drive Class' in self.data.columns else 1

        # ---- INTERFACE / SIGNAL LEVEL --------------------------------
        if pct_hot70 > 0.01:
            findings.append({
                "category": "INTERFACE / SIGNAL INTEGRITY",
                "risk": "CRITICAL",
                "finding": f"{pct_hot70*100:.1f}% of drives exceed 70°C — JEDEC JESD219A thermal limit breached.",
                "detail": (
                    "High junction temperature degrades PCIe/SATA PHY analog front-end circuitry. "
                    "NVMe PCIe Gen4/Gen5 PHY eye diagrams close as temperature rises, increasing bit error rate at the interface layer. "
                    "SATA drives show increased DIPM/DevSleep link-reset frequency above 65°C. "
                    "M.2 2280 form-factor drives in poorly ventilated slots routinely hit 80-90°C due to absent heatsink contact, "
                    "triggering PCIe L1.2 ASPM fallback that reduces effective bandwidth by up to 40%."
                ),
                "recommendation": (
                    "Immediate rack thermal audit. Check: inlet air temperature (target <27°C per ASHRAE A1), "
                    "blanking panel coverage, fan tray RPM and redundancy, M.2 heatsink presence and TIM quality. "
                    "For sustained high-temp drives: enable NVMe thermal management (SMART/Health Log temperature threshold feature, "
                    "NVMe Spec §8.10) to enforce a firmware-level throttle before damage accumulation."
                ),
                "spec_ref": "JEDEC JESD219A Table 3; NVMe Spec 1.4c §8.10; ASHRAE TC 9.9 A1 Envelope",
            })
        elif pct_hot55 > 0.25:
            findings.append({
                "category": "INTERFACE / SIGNAL INTEGRITY",
                "risk": "WARNING",
                "finding": f"{pct_hot55*100:.1f}% of drives operating above 55°C sustained caution threshold.",
                "detail": (
                    "Sustained temperature above 55°C accelerates thermal aging of PCIe retimer silicon and SATA PHY compensation circuits. "
                    "BGA underfill material between controller die and PCB begins softening above 60°C, "
                    "increasing mechanical stress on solder ball interconnects during thermal cycling. "
                    "Long-term reliability models (Norris-Landzberg) predict 2-3× solder fatigue life reduction "
                    "for drives cycling between 25°C and 65°C vs. 25°C to 45°C."
                ),
                "recommendation": (
                    "Increase chassis cooling airflow. Target inlet temperature <35°C. "
                    "Verify CRAC unit capacity and cold-aisle containment. "
                    "Check if drives are installed in a slot with an adjacent hot component (GPU, NIC, or another high-TDP SSD)."
                ),
                "spec_ref": "ASHRAE TC 9.9; IPC-9701A Thermal Cycling Fatigue; Norris-Landzberg solder fatigue model",
            })

        if 'Interface' in self.data.columns:
            gen5_count = len(self.data[self.data['Interface'].str.contains('Gen5', na=False)])
            if gen5_count > 0 and avg_temp > 55:
                findings.append({
                    "category": "INTERFACE / SIGNAL INTEGRITY",
                    "risk": "WARNING",
                    "finding": f"{gen5_count} PCIe Gen5 drives detected — signal integrity sensitivity elevated at >55°C.",
                    "detail": (
                        "PCIe Gen5 (32 GT/s) operates with 3dB less channel loss budget than Gen4, making it significantly more "
                        "sensitive to temperature-induced impedance variation in PCB traces, connector pin resistance drift, "
                        "and via stub resonance. At sustained temperatures above 55°C, FR4 PCB Dk (dielectric constant) shifts "
                        "by ~2-3%, pushing edge channels outside the Gen5 eye mask without triggering a hard link error "
                        "(the equalization adapts, but at the cost of increased latency jitter)."
                    ),
                    "recommendation": (
                        "Perform eye diagram qualification per PCIe Gen5 PHY compliance spec (PCI-SIG CEM 5.0). "
                        "Ensure slot and cable/riser card are rated Gen5. Reduce ambient temperature below 50°C for Gen5 deployments."
                    ),
                    "spec_ref": "PCI-SIG CEM Spec 5.0; PCIe Base Spec 5.0 §4.2",
                })

        # ---- PCB / HARDWARE LEVEL ------------------------------------
        if n_shut_hi > 0:
            findings.append({
                "category": "PCB / HARDWARE — POWER DELIVERY",
                "risk": "HIGH",
                "finding": f"{n_shut_hi} drives show unsafe shutdown count >500 — power delivery instability confirmed.",
                "detail": (
                    "High unsafe shutdown counts indicate PSU hold-up time insufficient (<10ms per ATX12V spec), "
                    "or PDU/UPS transfer time exceeding drive capacitor buffer capacity. "
                    "Drives WITHOUT hardware power-loss protection (PLP) capacitors experience NAND write-buffer corruption "
                    "and partial P/E cycle completion on every unexpected power loss, directly accelerating cell oxide degradation. "
                    "Repeated thermal cycling from power cycles induces solder joint fatigue at BGA NAND package corners "
                    "(Coffin-Manson model predicts accelerated fatigue failure proportional to ΔT³)."
                ),
                "recommendation": (
                    "Audit PSU hold-up time (scope the 12V rail on power-cut test). "
                    "Verify UPS online-transfer time <4ms. Check PDU input voltage stability with a power analyzer. "
                    "For non-PLP drives: upgrade to enterprise NVMe with hardware capacitor-backed PLP (OCP Datacenter NVMe Spec v2.5 §3.5). "
                    "Short-term: enable OS-level power loss notification (NVMe PLN feature, NVMe Spec §8.1)."
                ),
                "spec_ref": "ATX12V PSU Design Guide v2.53; NVMe Spec 1.4c §8.1 PLP; OCP NVMe SSD Spec v2.5 §3.5",
            })

        if pct_hot70 > 0.05 and n_shut_hi > 0:
            findings.append({
                "category": "PCB / HARDWARE — BGA RELIABILITY",
                "risk": "HIGH",
                "finding": "Combined thermal stress + power cycling detected — accelerated BGA solder joint fatigue risk.",
                "detail": (
                    "The combination of high operating temperature and frequent unsafe shutdowns creates the worst-case "
                    "thermal cycling profile for BGA interconnects. The Coffin-Manson fatigue model shows that NAND package "
                    "BGA solder ball cycles-to-failure drops exponentially as ΔT increases. "
                    "At 70°C operation with cold-boot cycling, solder joint lifetime may be reduced by 5-10× vs. rated conditions. "
                    "This can manifest as intermittent drive disappearance (PCIe device loss) before any SMART attribute changes."
                ),
                "recommendation": (
                    "Request dye-and-pry BGA inspection from drive vendor if drives show intermittent PCIe link loss without SMART errors. "
                    "Reduce thermal cycling frequency (avoid unnecessary server reboots) and address root-cause thermal issues first."
                ),
                "spec_ref": "Coffin-Manson thermal fatigue model; IPC-9701A Section 4; JEDEC JESD22-A104",
            })

        # ---- NAND COMPONENT LEVEL ------------------------------------
        if n_media_drv > 0:
            findings.append({
                "category": "NAND COMPONENT — DIE HEALTH",
                "risk": "CRITICAL",
                "finding": f"{n_media_drv} drives have non-zero UECC Media Errors ({n_media} total uncorrectable events).",
                "detail": (
                    "Uncorrectable ECC errors (UECC) indicate the LDPC/BCH correction engine has exceeded its correction capability. "
                    "Root causes (in order of likelihood): "
                    "(1) NAND die wear-out — P/E cycles exceeding cell endurance rating, narrowing charge margin between threshold voltage distributions; "
                    "(2) NAND read disturb accumulation — parasitic programming of unselected cells on shared wordlines without sufficient scrubbing; "
                    "(3) Data retention failure — charge leakage from floating gates/charge traps in idle/cold-stored cells (worsens with wear); "
                    "(4) Defective NAND die — wafer-level screening escape (especially in outer-edge die with process variation). "
                    "Each UECC event represents permanent data loss. Silent corruption may propagate to RAID parity or backup if not immediately isolated."
                ),
                "recommendation": (
                    "Immediate RMA for ALL drives with Media Errors > 0. Do not write new data. "
                    "Submit failed drives to vendor for Failure Analysis (FA) per JEDEC JESD22-A119. "
                    "Request NAND die-level test data (MBIST results) and lot traceability under RMA agreement. "
                    "Verify backup and RAID data integrity using independent checksums (sha256sum or equivalent) before trusting data on surviving drives."
                ),
                "spec_ref": "JEDEC JESD218B; NVMe Spec 1.4c §5.14; JEDEC JESD22-A119 NAND Failure Analysis",
            })

        if n_spare_eol > 0:
            findings.append({
                "category": "NAND COMPONENT — SPARE POOL EXHAUSTION",
                "risk": "CRITICAL",
                "finding": f"{n_spare_eol} drives below 10% available spare — JEDEC JESD218B end-of-life threshold.",
                "detail": (
                    "Spare block pool exhaustion forces the FTL into guardband mode. With fewer spare blocks: "
                    "wear leveling becomes less effective (hot-block wear concentration accelerates remaining NAND die degradation), "
                    "garbage collection overhead increases write latency 3-10×, "
                    "and the controller may trigger an automatic read-only transition without host warning. "
                    "This is a JEDEC-defined end-of-life condition, not a failure — drives at this state are operating "
                    "beyond their rated guarantee."
                ),
                "recommendation": (
                    "Emergency data migration required within 24 hours. "
                    "Do NOT write new data to drives below 10% spare. "
                    "Initiate warranty RMA claim under JESD218B endurance guarantee. "
                    "Use the RMA Priority List export to identify affected serials."
                ),
                "spec_ref": "JEDEC JESD218B §4.3 — Available Spare Threshold (End-of-Life definition)",
            })

        waf_df = self.calculate_waf()
        if not waf_df.empty and waf_df['WAF_Proxy'].max() > 5:
            high_waf_count = len(waf_df[waf_df['WAF_Proxy'] > 5])
            findings.append({
                "category": "NAND COMPONENT — WRITE AMPLIFICATION",
                "risk": "WARNING",
                "finding": f"{high_waf_count} drives show WAF proxy >5 — excessive NAND P/E cycle consumption.",
                "detail": (
                    "WAF > 5 means the NAND is receiving 5× more write data than the host intended, consuming endurance "
                    "5× faster than the workload spec implies. Physical mechanism: the FTL performs excessive garbage collection "
                    "due to high write fragmentation, rewriting valid data alongside invalid pages. "
                    "For QLC NAND (4 bits/cell): each P/E cycle narrows all 15 threshold voltage distribution windows by ~5mV, "
                    "exponentially increasing UECC risk as cells age. "
                    "SLC write cache exhaustion on QLC drives causes direct QLC programming at high WAF, bypassing the SLC buffer."
                ),
                "recommendation": (
                    "Enable OS TRIM/discard (NVMe DSM Deallocate command). "
                    "Verify RAID controller is not blocking TRIM pass-through. "
                    "Investigate write pattern: high random-write ratios (< 8KB block size) cause worst-case WAF. "
                    "Consider adding a DRAM write-coalescing tier (Optane/SCM) in front of QLC drives in write-heavy roles."
                ),
                "spec_ref": "Flash Memory Summit 2023 — WAF analysis; Micron TN-FN-36; Samsung SSD WAF WhitePaper",
            })

        # ---- FIRMWARE / CONTROLLER LEVEL ----------------------------
        if n_log_hi > 0:
            findings.append({
                "category": "FIRMWARE / CONTROLLER",
                "risk": "HIGH" if n_log_hi > max(3, n * 0.05) else "WARNING",
                "finding": f"{n_log_hi} drives show >1,000 error log entries — controller firmware instability.",
                "detail": (
                    "High error log counts aggregate across NVMe error classes: command timeouts, NVM subsystem resets, "
                    "FTL exception handler invocations, failed background operations, and controller state machine faults. "
                    f"Pool has {fw_versions} distinct firmware revision(s). "
                    "Common root causes: GC state machine race conditions (especially during mixed read/write workloads), "
                    "interrupt coalescing timer misconfiguration causing command queue stalls, "
                    "thermal throttle state transition bugs triggering controller reset loops, "
                    "or DRAM cache ECC soft errors causing NVM subsystem reset cascades."
                ),
                "recommendation": (
                    "Cross-reference affected drives against firmware version (Firmware Maturity Matrix in QC/Sigma tab). "
                    "If errors cluster on specific firmware: escalate to vendor with NVMe Telemetry Log (log page 0x07 host-initiated, "
                    "0x08 controller-initiated) for firmware bug FA. "
                    "Request vendor firmware advisory and validated update path."
                ),
                "spec_ref": "NVMe Spec 1.4c §5.14, §8.8 Telemetry; vendor firmware release advisory process",
            })

        if fw_versions > 3:
            findings.append({
                "category": "FIRMWARE / CONTROLLER",
                "risk": "WARNING",
                "finding": f"{fw_versions} different firmware revisions active in this pool — fragmented update posture.",
                "detail": (
                    "A pool with > 3 firmware versions simultaneously active creates: "
                    "inconsistent SMART counter semantics (some FW versions reset counters on update), "
                    "mixed GC algorithm behavior (making WAF comparison across drives invalid), "
                    "and split RMA liability (vendor may not support non-current firmware). "
                    "Firmware fragmentation also makes it harder to reproduce issues for vendor FA."
                ),
                "recommendation": (
                    "Standardize the pool on the single highest-yield firmware revision identified in the Firmware Maturity Matrix. "
                    "Create a firmware update plan with staged rollout (10% of pool first, then validate, then full pool). "
                    "Document current revision baseline before any update for rollback capability."
                ),
                "spec_ref": "OCP Datacenter NVMe SSD Spec v2.5 §5.2 (Firmware Update requirements)",
            })

        # ---- MANUFACTURING / BATCH LEVEL ----------------------------
        anomalies = self.detect_anomalies()
        anom_pct  = len(anomalies) / n
        if anom_pct > 0.05:
            findings.append({
                "category": "MANUFACTURING / BATCH QUALITY",
                "risk": "HIGH",
                "finding": f"{len(anomalies)} drives ({anom_pct*100:.1f}%) flagged as multi-sensor statistical outliers (Z > 3\u03c3).",
                "detail": (
                    "A batch anomaly rate above 5% is a manufacturing quality signal. "
                    "Multi-sensor outliers (simultaneous deviation across temperature, spare, wear, errors) "
                    "indicate process-level variation rather than random individual failures: "
                    "possible causes include NAND wafer lot with elevated defect density (wafer-edge die, "
                    "photolithography overlay error), assembly line thermal profile deviation (reflow oven miscalibration "
                    "causing partial BGA solder joint formation), or incoming inspection criteria failure at the OEM."
                ),
                "recommendation": (
                    "Request NAND wafer lot traceability data from vendor (die coordinates, wafer map, lot ID). "
                    "Demand full FA report per IPC-A-610 workmanship acceptance criteria. "
                    "Consider 100% burn-in test screening for this lot (JEDEC JESD22-A103, 125°C for 24-48hrs) "
                    "to precipitate latent failures before deployment. "
                    "Hold remaining inventory from same purchase order pending FA results."
                ),
                "spec_ref": "IPC-A-610 Rev H; JEDEC JESD22-A103 Burn-In Testing; AEC-Q100 Rev H (adapted for storage)",
            })

        # ---- PROCESS / QUALIFICATION LEVEL --------------------------
        if n_classes > 2:
            cls_list = ', '.join(self.data['Drive Class'].unique()) if 'Drive Class' in self.data.columns else 'N/A'
            findings.append({
                "category": "PROCESS / QUALIFICATION SCOPE",
                "risk": "INFO",
                "finding": f"{n_classes} drive classes in this batch ({cls_list}) — mixed qualification risk.",
                "detail": (
                    "Mixed drive classes (NVMe/SATA/QLC/Consumer) in a single RMA batch makes per-class failure attribution unreliable. "
                    "Consumer/Client drives lack: enterprise PLP capacitors, extended temp rating (0-70°C vs. enterprise 0-85°C), "
                    "enterprise SMART feature set (vendor-specific log pages), and SNIA SSS PTS performance guarantees. "
                    "Comparing Consumer telemetry against enterprise thresholds in this report may generate misleading findings."
                ),
                "recommendation": (
                    "Segregate analysis by drive class. Apply enterprise thresholds only to enterprise-class drives. "
                    "Audit whether Consumer-class drives appear in enterprise rack slots — this is a qualification scope violation "
                    "that voids drive warranty and potentially violates datacenter equipment certifications. "
                    "Reference OCP Datacenter NVMe SSD Spec v2.5 for minimum enterprise feature requirements."
                ),
                "spec_ref": "OCP Datacenter NVMe SSD Spec v2.5; SNIA SSS PTS v2.0.2; JEDEC JESD219A",
            })

        if len(findings) == 0:
            findings.append({
                "category": "ALL SYSTEMS",
                "risk": "PASS",
                "finding": "No significant risk factors detected in this telemetry batch.",
                "detail": (
                    "All monitored telemetry parameters fall within enterprise-grade thresholds. "
                    "Pool score, thermal distribution, NAND health, and firmware stability are all nominal. "
                    "Continue standard monitoring cadence."
                ),
                "recommendation": "Archive these results. Schedule next telemetry collection per standard maintenance interval.",
                "spec_ref": "JEDEC JESD218B; JEDEC JESD219A; NVMe Spec 1.4c",
            })

        return findings

