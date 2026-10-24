-- Run once as a MySQL admin (e.g. `mysql -u root -p < mysql_setup.sql`).
-- Tables are created automatically by the app on first connect.
CREATE DATABASE IF NOT EXISTS offlinepos_central
  CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

-- Use 'localhost' if MySQL runs on the same machine, or '%' for remote access.
CREATE USER IF NOT EXISTS 'offlinepos'@'localhost' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON offlinepos_central.* TO 'offlinepos'@'localhost';
FLUSH PRIVILEGES;
