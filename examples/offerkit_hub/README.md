# Feeding an offerkit Data hub

etl+ writes Parquet files into a folder; offerkit's Data hub reads that folder as a
`parquet` source. Nothing else connects the two: no shared database, no shared code.

```
core (postgres, read-only)  --etl+-->  ./hub/*.parquet  --offerkit hub.yaml-->  std_* tables
```

1. Copy this folder, set the `core` host and user in `connections.yaml`, and give the
   password once in the console (or `CORE_PASSWORD` in the environment).
2. `etlplus run --all --project .` runs every job; each writes `hub/<table>.parquet`, published
   only when complete (written as `.partial`, then renamed).
3. In offerkit's `hub.yaml`, point a source at the folder and map the tables, as in
   `hub.yaml.example` here.

Transactions are aggregated before they leave the source (customer x day x code x
direction), so no statement line and no narrative lands on disk.
