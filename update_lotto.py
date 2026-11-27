from lotto_data import update_history
import argparse
if __name__ == "__main__":
    p=argparse.ArgumentParser(); p.add_argument("--years",type=int,choices=[1,2,3,5],default=5); args=p.parse_args()
    data,msg,ok=update_history(args.years); print(msg); raise SystemExit(0 if ok else 1)
