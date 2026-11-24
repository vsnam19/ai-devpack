void work(void) {
 open("/etc/sample.conf", O_RDONLY);
 ioctl(fd, SAMPLE_QUERY, &value);
 socket(AF_UNIX, SOCK_STREAM, 0);
 getenv("MODE");
#ifdef SIMULATION
 open("/tmp/fake-device", O_RDWR);
#else
 open("/dev/sample", O_RDWR);
#endif
}
