# Risk Router external blind evaluation v1

- Freeze: `external-blind-v1-2dd91c8b9acf`
- Rows: 40
- Dataset SHA-256: `2dd91c8b9acfa7423c1e1818efa880e33e6f3c3a2be1bc8dd2f035e92a214abc`
- Coverage: 97.5%
- Strict accuracy: 50.0%
- Covered accuracy: 51.3%
- Risk recall: 100.0%
- NON_TARGET false-risk rate: 95.0%
- Gate: FAIL
- Promotion: `REMAIN_SHADOW`

The set was frozen with expected labels and source bytes before inference. It has zero title/ID overlap with the training corpus and is not eligible for retraining model v1.

## Gate details

- PASS — `minimum_rows`
- PASS — `coverage`
- FAIL — `covered_accuracy`
- PASS — `risk_recall`
- FAIL — `non_target_false_risk_rate`
- PASS — `zero_training_overlap`
- PASS — `label_first_freeze`

## Errors and abstentions

- `EXT-03d2c947fe2db2ee7ba5` NON_TARGET -> RISK_REVIEW (68.0%) — Nemotron Labs: How Open Models Give Enterprises and Nations AI They Can Trust, Control and Customize
- `EXT-0a7412f811df6e3454ea` NON_TARGET -> RISK_REVIEW (74.8%) — Japan’s Robotics and Manufacturing Leaders Build on NVIDIA Cosmos to Advance Physical AI Frontier
- `EXT-133a970622dc59b62e2e` NON_TARGET -> RISK_REVIEW (76.4%) — NVIDIA Nemotron Achieves Benchmark-Leading Performance With LangChain Deep Agents Harness
- `EXT-1984cc473844f50a4f15` NON_TARGET -> RISK_REVIEW (64.0%) — NVIDIA Unlocks AI Compute at Scale, Inviting Capital Partners to Power the AI Infrastructure Buildout
- `EXT-3266e592e9012e7ab6fc` NON_TARGET -> RISK_REVIEW (74.8%) — How Open Models Are Driving AI Research
- `EXT-41fb95adcac441610dec` NON_TARGET -> ABSTAIN (53.5%) — Why Performance per Watt Is the Ultimate Metric for AI Infrastructure Efficiency
- `EXT-48c5cd1cbd7ed28df113` NON_TARGET -> RISK_REVIEW (62.9%) — NVIDIA and Hugging Face Bring New Models and Frameworks to LeRobot for the Open Robotics Community
- `EXT-52a263d8d3a71dfeea4a` NON_TARGET -> RISK_REVIEW (67.0%) — How Nations Are Deploying AI for Strategic Priorities
- `EXT-56ee56dd0f49b2600dbe` NON_TARGET -> RISK_REVIEW (70.4%) — Joyride Through July With 12 Games Coming to GeForce NOW
- `EXT-7f3768ee857027ff6e3a` NON_TARGET -> RISK_REVIEW (79.2%) — Agencies issue joint statement on handling of highly sensitive information during bank examinations
- `EXT-86f750304bb3cb6392e1` NON_TARGET -> RISK_REVIEW (74.9%) — Japan’s Enterprises and Startups Build Industry-Specialized AI With NVIDIA Nemotron Open Models
- `EXT-9455abdc90f550168f7f` NON_TARGET -> RISK_REVIEW (76.2%) — GeForce NOW Turns Up the Heat With New GeForce RTX 5080-Powered Toronto Server
- `EXT-9c74e321b6de6895e744` NON_TARGET -> RISK_REVIEW (77.0%) — Federal Reserve notes with deep sadness the passing of Alan Greenspan
- `EXT-aab15e1898381a39d57f` NON_TARGET -> RISK_REVIEW (67.2%) — AI Innovators Adopt NVIDIA Vera — Why Max Single-Threaded CPU at Scale Matters
- `EXT-b08973dc78ebfc965c67` NON_TARGET -> RISK_REVIEW (71.8%) — Japan Government, Industrial Leaders and NVIDIA Launch the World’s First National AI Infrastructure
- `EXT-bc8d4fd386edbafc3c92` NON_TARGET -> RISK_REVIEW (70.5%) — Federal Reserve issues initial findings from its 2025 triennial payments study
- `EXT-d4cfe858b14ae3635683` NON_TARGET -> RISK_REVIEW (82.4%) — NVIDIA Vera Rubin Maximizes Intelligence per Dollar for Post-Training Workloads — a Key Metric for Agentic AI
- `EXT-df6158c65d5ebef2cfe2` NON_TARGET -> RISK_REVIEW (62.2%) — NVIDIA Introduces New Jetson Thor Computers to Advance Mainstream Robotics and Edge AI
- `EXT-e5f34023ba49e3e0d08d` NON_TARGET -> RISK_REVIEW (66.6%) — Sharpen the Sword, Skip the Downloads — ‘Onimusha: Way of the Sword’ Is Coming to GeForce NOW
- `EXT-f03c112a6a86b1160ab0` NON_TARGET -> RISK_REVIEW (77.2%) — NVIDIA and Japan Bring Full-Stack AI and Robotics to Every Industry
