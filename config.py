from pyspark.sql import SparkSession
import logging

logger = logging.getLogger("ConfigLoader")
spark = SparkSession.builder.getOrCreate()

try:
    from pyspark.dbutils import DBUtils
    dbutils = DBUtils(spark)
except:
    pass

KAFKA_BOOTSTRAP_SERVERS = "pkc-921jm.us-east-2.aws.confluent.cloud:9092"
KAFKA_TOPIC = "flights_data"

dbutils.widgets.text("KAFKA_KEY", "")
dbutils.widgets.text("KAFKA_SECRET", "")

KAFKA_API_KEY = dbutils.widgets.get("KAFKA_KEY")
KAFKA_API_SECRET = dbutils.widgets.get("KAFKA_SECRET")

kafka_spark_options = {
    "kafka.bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
    "kafka.security.protocol": "SASL_SSL",
    "kafka.sasl.jaas.config": f'kafkashaded.org.apache.kafka.common.security.plain.PlainLoginModule required username="{KAFKA_API_KEY}" password="{KAFKA_API_SECRET}";',
    "kafka.sasl.mechanism": "PLAIN",
    "subscribe": KAFKA_TOPIC,
    "startingOffsets": "earliest"
}