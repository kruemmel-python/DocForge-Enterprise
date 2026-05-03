-- MySQL dump 10.13  Distrib 8.0.36, for Win64 (x86_64)
--
-- Host: 127.0.0.1    Database: arbeitslosenzahlen
-- ------------------------------------------------------
-- Server version	8.0.36

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `arbeitsmarktstatistik`
--

DROP TABLE IF EXISTS `arbeitsmarktstatistik`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `arbeitsmarktstatistik` (
  `Jahr` int NOT NULL,
  `FrueheresBundesgebiet_ID` int DEFAULT NULL,
  `NeueLaender_ID` int DEFAULT NULL,
  PRIMARY KEY (`Jahr`),
  KEY `FrueheresBundesgebiet_ID` (`FrueheresBundesgebiet_ID`),
  KEY `NeueLaender_ID` (`NeueLaender_ID`),
  CONSTRAINT `arbeitsmarktstatistik_ibfk_1` FOREIGN KEY (`FrueheresBundesgebiet_ID`) REFERENCES `frueheresbundesgebiet` (`Jahr`),
  CONSTRAINT `arbeitsmarktstatistik_ibfk_2` FOREIGN KEY (`NeueLaender_ID`) REFERENCES `neuelaender` (`Jahr`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `arbeitsmarktstatistik`
--

LOCK TABLES `arbeitsmarktstatistik` WRITE;
/*!40000 ALTER TABLE `arbeitsmarktstatistik` DISABLE KEYS */;
INSERT INTO `arbeitsmarktstatistik` VALUES (1991,1991,1991),(1992,1992,1992),(1993,1993,1993),(1994,1994,1994),(1995,1995,1995),(1996,1996,1996),(1997,1997,1997),(1998,1998,1998),(1999,1999,1999),(2000,2000,2000),(2001,2001,2001),(2002,2002,2002),(2003,2003,2003),(2004,2004,2004),(2005,2005,2005),(2006,2006,2006),(2007,2007,2007),(2008,2008,2008),(2009,2009,2009),(2010,2010,2010),(2011,2011,2011),(2012,2012,2012),(2013,2013,2013),(2014,2014,2014),(2015,2015,2015),(2016,2016,2016),(2017,2017,2017),(2018,2018,2018),(2019,2019,2019),(2020,2020,2020),(2021,2021,2021),(2022,2022,2022),(2023,2023,2023);
/*!40000 ALTER TABLE `arbeitsmarktstatistik` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2025-11-15 22:37:30
