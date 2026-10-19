from pyspark.sql.functions import col

bronze_df = spark.read.table("workspace.flights_pipeline.incoming_flights")

silver_df = (bronze_df
    .dropDuplicates(["icao24"]) 
    .filter(col("callsign").isNotNull())
)

silver_df.write.format("delta").mode("overwrite").saveAsTable("workspace.flights_pipeline.silver_flights")