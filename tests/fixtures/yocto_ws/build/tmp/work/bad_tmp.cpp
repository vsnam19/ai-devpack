int buildTmpBroken(int flag) {
    int* ptr = nullptr;
    if (flag > 0) {
        return *ptr;
    }
    return (int)1.5;
}
