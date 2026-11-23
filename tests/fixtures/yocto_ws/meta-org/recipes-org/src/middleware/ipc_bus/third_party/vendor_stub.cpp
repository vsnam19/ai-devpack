int thirdPartyStub(int flag) {
    int* ptr = nullptr;
    if (flag > 0) {
        return *ptr;
    }
    return (int)9.0;
}
