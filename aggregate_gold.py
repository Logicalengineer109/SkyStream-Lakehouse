from pyspark.sql.functions import col, count, avg, max, min, round, when

silver_df = spark.read.table("workspace.flights_pipeline.silver_flights")

gold_df = (silver_df
    .groupBy("origin_country")
    .agg(
        count("*").alias("total_aircrafts"),
        
        round(avg("altitude"), 2).alias("avg_altitude"),
        max("altitude").alias("max_record_altitude"),
        
        round(avg("velocity"), 2).alias("avg_velocity_mps"),
        
        count(when(col("on_ground") == True, 1)).alias("aircrafts_on_ground"),
        count(when(col("on_ground") == False, 1)).alias("aircrafts_in_air")
    )
    .filter(col("total_aircrafts") > 1)  
    .orderBy(col("avg_velocity_mps").desc())
)

anomalies_df = (silver_df
    .filter((col("altitude") < 500) & (col("on_ground") == False))
    .select("icao24", "callsign", "origin_country", "altitude")
)

gold_df.write.format("delta").mode("overwrite").saveAsTable("workspace.flights_pipeline.gold_fleet_statistics")
anomalies_df.write.format("delta").mode("overwrite").saveAsTable("workspace.flights_pipeline.gold_low_altitude_alerts")

print("Gold tables updated: fleet statistics and safety alerts.")